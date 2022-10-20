import math
from functools import partial
from typing import Optional, List

import torch
import torch.nn.functional as F
from torch import nn, einsum

from einops import rearrange, repeat
from einops.layers.torch import Rearrange

from vector_quantize_pytorch import VectorQuantize

from phenaki_pytorch.t5 import t5_encode_text, get_encoded_dim, DEFAULT_T5_NAME

# helpers

def exists(val):
    return val is not None

def default(val, d):
    return val if exists(val) else d

# classifier free guidance functions

def prob_mask_like(shape, prob, device):
    if prob == 1:
        return torch.ones(shape, device = device, dtype = torch.bool)
    elif prob == 0:
        return torch.zeros(shape, device = device, dtype = torch.bool)
    else:
        return torch.zeros(shape, device = device).float().uniform_(0, 1) < prob

# feedforward

def FeedForward(dim, mult = 4):
    inner_dim = int(mult * dim)
    return nn.Sequential(
        nn.LayerNorm(dim),
        nn.Linear(dim, inner_dim, bias = False),
        nn.GELU(),
        nn.Linear(inner_dim, dim, bias = False)
    )

# attention

class Attention(nn.Module):
    def __init__(
        self,
        dim,
        dim_context = None,
        dim_head = 64,
        heads = 8,
        causal = False,
        num_null_kv = 0
    ):
        super().__init__()
        self.heads = heads
        self.causal = causal
        self.scale = dim_head ** -0.5
        inner_dim = dim_head * heads
        dim_context = default(dim_context, dim)

        self.norm = nn.LayerNorm(dim)

        self.null_kv = nn.Parameter(torch.randn(heads, 2 * num_null_kv, dim_head))

        self.to_q = nn.Linear(dim, inner_dim, bias = False)
        self.to_kv = nn.Linear(dim_context, inner_dim * 2, bias = False)
        self.to_out = nn.Linear(inner_dim, dim, bias = False)

    def forward(
        self,
        x,
        mask = None,
        context = None
    ):
        batch, device, dtype = x.shape[0], x.device, x.dtype

        kv_input = default(context, x)

        x = self.norm(x)

        q, k, v = self.to_q(x), *self.to_kv(kv_input).chunk(2, dim = -1)

        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = self.heads), (q, k, v))

        q = q * self.scale

        nk, nv = repeat(self.null_kv, 'h (n r) d -> b h n r d', b = batch, r = 2).unbind(dim = -2)

        k = torch.cat((nk, k), dim = -2)
        v = torch.cat((nv, v), dim = -2)

        sim = einsum('b h i d, b h j d -> b h i j', q, k)

        i, j = sim.shape[-2:]

        if exists(mask):
            mask = F.pad(mask, (j - mask.shape[-1], 0), value = True)
            mask = rearrange(mask, 'b j -> b 1 1 j')
            sim = sim.masked_fill(~mask, -torch.finfo(sim.dtype).max)

        if self.causal:
            causal_mask = torch.ones((i, j), device = device, dtype = torch.bool).triu(j - i + 1)
            sim = sim.masked_fill(causal_mask, -torch.finfo(sim.dtype).max)

        attn = sim.softmax(dim = -1)

        out = einsum('b h i j, b h j d -> b h i d', attn, v)

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

# transformer

class Transformer(nn.Module):
    def __init__(
        self,
        dim,
        *,
        depth,
        dim_context = None,
        causal = False,
        dim_head = 64,
        heads = 8,
        ff_mult = 4,
        attn_num_null_kv = 2,
        has_cross_attn = False
    ):
        super().__init__()
        self.layers = nn.ModuleList([])

        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                Attention(dim = dim, dim_head = dim_head, heads = heads, causal = causal),
                Attention(dim = dim, dim_head = dim_head, dim_context = dim_context, heads = heads, causal = False, num_null_kv = attn_num_null_kv) if has_cross_attn else None,
                FeedForward(dim = dim, mult = ff_mult)
            ]))

        self.norm_out = nn.LayerNorm(dim)

    def forward(self, x, context = None, mask = None):

        for self_attn, cross_attn, ff in self.layers:
            x = self_attn(x) + x

            if exists(cross_attn) and exists(context):
                x = cross_attn(x, context = context, mask = None) + x

            x = ff(x) + x

        return self.norm_out(x)

# c-vivit - 3d ViT with factorized spatial and temporal attention made into an vqgan-vae autoencoder

class CViViT(nn.Module):
    def __init__(
        self,
        *,
        dim,
        codebook_size,
        patch_size,
        temporal_patch_size,
        spatial_depth,
        temporal_depth,
        dim_head = 64,
        heads = 8,
        channels = 3
    ):
        """
        einstein notations:

        b - batch
        c - channels
        t - time
        d - feature dimension
        p1, p2, pt - image patch sizes and then temporal patch size
        """

        super().__init__()

        self.to_patch_emb_first_frame = nn.Sequential(
            Rearrange('b c 1 (h p1) (w p2) -> b 1 h w (c p1 p2)', p1 = patch_size, p2 = patch_size),
            nn.Linear(channels * patch_size * patch_size, dim)
        )

        self.to_patch_emb = nn.Sequential(
            Rearrange('b c (t pt) (h p1) (w p2) -> b t h w (c pt p1 p2)', p1 = patch_size, p2 = patch_size, pt = temporal_patch_size),
            nn.Linear(channels * patch_size * patch_size * temporal_patch_size, dim)
        )

        self.enc_spatial_transformer = Transformer(dim = dim, depth = spatial_depth, dim_head = dim_head, heads = heads)
        self.enc_temporal_transformer = Transformer(dim = dim, depth = temporal_depth, dim_head = dim_head, heads = heads, causal = True)

        self.vq = VectorQuantize(dim = dim, codebook_size = codebook_size, use_cosine_sim = True)

        self.dec_spatial_transformer = Transformer(dim = dim, depth = spatial_depth, dim_head = dim_head, heads = heads)
        self.dec_temporal_transformer = Transformer(dim = dim, depth = temporal_depth, dim_head = dim_head, heads = heads, causal = True)

        self.to_pixels_first_frame = nn.Sequential(
            nn.Linear(dim, channels * patch_size * patch_size),
            Rearrange('b 1 h w (c p1 p2) -> b c 1 (h p1) (w p2)', p1 = patch_size, p2 = patch_size)
        )

        self.to_pixels = nn.Sequential(
            nn.Linear(dim, channels * patch_size * patch_size * temporal_patch_size),
            Rearrange('b t h w (c pt p1 p2) -> b c (t pt) (h p1) (w p2)', p1 = patch_size, p2 = patch_size, pt = temporal_patch_size),
        )

    def forward(
        self,
        video,
        return_loss = False,
        return_only_codebook_ids = False
    ):
        assert video.ndim == 5
        b, c, f, *_ = video.shape

        first_frame, rest_frames = video[:, :, :1], video[:, :, 1:]

        # derive patches

        first_frame_tokens = self.to_patch_emb_first_frame(first_frame)
        rest_frames_tokens = self.to_patch_emb(rest_frames)

        tokens = torch.cat((first_frame_tokens, rest_frames_tokens), dim = 1)

        # save height and width in

        shape = tokens.shape
        *_, h, w, _ = shape

        # encode - spatial

        tokens = rearrange(tokens, 'b t h w d -> (b t) (h w) d')

        tokens = self.enc_spatial_transformer(tokens)

        tokens = rearrange(tokens, '(b t) (h w) d -> b t h w d', b = b, h = h , w = w)

        # encode - temporal

        tokens = rearrange(tokens, 'b t h w d -> (b h w) t d')

        tokens = self.enc_temporal_transformer(tokens)

        tokens = rearrange(tokens, '(b h w) t d -> b t h w d', b = b, h = h, w = w)

        # quantize

        tokens = rearrange(tokens, 'b t h w d -> b (t h w) d')

        tokens, indices, commit_loss = self.vq(tokens)

        if return_only_codebook_ids:
            return indices

        tokens = rearrange(tokens, 'b (t h w) d -> b t h w d', h = h, w = w)

        # decode - temporal

        tokens = rearrange(tokens, 'b t h w d -> (b h w) t d')

        tokens = self.dec_temporal_transformer(tokens)

        tokens = rearrange(tokens, '(b h w) t d -> b t h w d', b = b, h = h, w = w)

        # decode - spatial

        tokens = rearrange(tokens, 'b t h w d -> (b t) (h w) d')

        tokens = self.dec_spatial_transformer(tokens)

        tokens = rearrange(tokens, '(b t) (h w) d -> b t h w d', b = b, h = h , w = w)

        # to pixels

        first_frame_token, rest_frames_tokens = tokens[:, :1], tokens[:, 1:]

        first_frame = self.to_pixels_first_frame(first_frame_token)

        rest_frames = self.to_pixels(rest_frames_tokens)

        recon_video = torch.cat((first_frame, rest_frames), dim = 2)

        if not return_loss:
            return x

        return F.mse_loss(video, recon_video)

# mask git

class MaskGit(nn.Module):
    def __init__(
        self,
        *,
        dim,
        num_tokens,
        max_seq_len,
        **kwargs
    ):
        super().__init__()
        self.mask_id = num_tokens

        self.token_emb = nn.Embedding(num_tokens + 1, dim) # last token is used as mask_id
        self.pos_emb = nn.Embedding(max_seq_len, dim)

        self.transformer = Transformer(
            dim = dim,
            attn_num_null_kv = 2,
            has_cross_attn = True,
            **kwargs
        )

        self.to_logits = nn.Linear(dim, num_tokens)

    def forward(self, x, **kwargs):
        n, device = x.shape[1], x.device

        x = self.token_emb(x)
        x = self.pos_emb(torch.arange(n, device = device)) + x

        x = self.transformer(x, **kwargs)

        return self.to_logits(x)

class MaskGitTrainWrapper(nn.Module):
    def __init__(
        self,
        maskgit,
        *,
        steps
    ):
        super().__init__()
        self.maskgit = maskgit
        self.mask_id = maskgit.mask_id

        self.steps = steps

    def forward(self, x, **kwargs):
        batch, seq, device = *x.shape, x.device

        rand_step = torch.randint(0, self.steps, (1,), device = device)
        num_tokens_mask = (seq * torch.cos(rand_step * math.pi * 0.5 / self.steps)).round().long().clamp(min = 1) # cosine schedule was best

        _, indices = torch.randn((batch, seq), device = device).topk(num_tokens_mask.item(), dim = -1)
        mask = torch.zeros((batch, seq), device = device).scatter(1, indices, 1.).bool()

        masked_input = torch.where(mask, self.steps, x)

        logits = self.maskgit(masked_input, **kwargs)

        loss = F.cross_entropy(logits[mask], x[mask])
        return loss

# main class

class Phenaki(nn.Module):
    def __init__(
        self,
        *,
        maskgit,
        steps = 100,
        cvivit = None,
        t5_name = DEFAULT_T5_NAME,
        text_embed_dim = None,
        cond_drop_prob = 0.25,
        max_text_len = 128
    ):
        super().__init__()

        self.cvivit = cvivit

        self.maskgit = maskgit
        self.maskgit_trainer = MaskGitTrainWrapper(maskgit, steps = steps)

        # text conditioning

        text_embed_dim = default(text_embed_dim, get_encoded_dim(t5_name))
        self.encode_texts = partial(t5_encode_text, name = t5_name)
        self.text_embed_dim = text_embed_dim
        self.max_text_len = max_text_len

        assert cond_drop_prob > 0.
        self.cond_drop_prob = cond_drop_prob # classifier free guidance for transformers - @crowsonkb

    def sample(
        self,
        *,
        texts: List[str],
        scene_lengths: List[int]
    ):
        raise NotImplementedError

    def forward(
        self,
        videos = None,
        texts: Optional[List[str]] = None,
        video_codebook_ids = None,
        text_embeds = None,
        cond_drop_prob = None
    ):
        assert exists(videos) ^ exists(video_codebook_ids), 'either raw video or '
        assert not (exists(videos) and not exists(self.cvivit)), 'cvivit must be provided if one wants to encode the videos live during training'

        assert exists(text_embeds) ^ exists(texts), 'either raw text of text embeds must be given'
        assert not (exists(text_embeds) and text_embeds.shape[-1] != self.text_embed_dim), 'text embedding dimension is not correct'

        if not exists(video_codebook_ids):
            with torch.no_grad():
                self.cvivit.eval()
                video_codebook_ids = self.cvivit(videos, return_only_codebook_ids = True)

        if not exists(text_embeds):
            text_embeds = self.encode_texts(texts, output_device = video_codebook_ids.device)

        batch, device = text_embeds.shape[0], text_embeds.device

        text_mask = torch.any(text_embeds != 0, dim = -1) # save the researcher from having to think about mask, by assuming if all of the feature dimension is 0, it is masked out

        # condition dropout for Katherine's (@crowsonkb) version of classifier free guidance for transformers

        cond_drop_prob = default(cond_drop_prob, self.cond_drop_prob)

        if cond_drop_prob > 0:
            keep_mask = prob_mask_like((batch,), 1 - cond_drop_prob, device = device)
            text_mask = rearrange(keep_mask, 'b -> b 1') & text_mask

        # train maskgit with text condition

        loss = self.maskgit_trainer(
            video_codebook_ids,
            mask = text_mask,
            context = text_embeds
        )

        return loss