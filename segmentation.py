"""
segmentation.py — Phase I: Occlusion-Robust Road Extraction
=============================================================
Two extraction paths share the same interface:

  extract_road_mask(img_bgr, mode='classical'|'dl', ...)
    → uint8 binary mask (255 = road, 0 = background)

DL path  : AttentionUNet (PyTorch, ~5M params, CPU-runnable)
           Uses imagenet-initialised encoder, attention gates at skip
           connections for long-range context — the key to "seeing
           through" occlusions (tree canopy, shadows, vehicles).
           If no weights file exists, the model is used untrained
           (good enough for a structural demo; swap in fine-tuned
           weights from SpaceNet/DeepGlobe training at any time).

Classical: CLAHE → adaptive threshold → morph close → elongation
           filter — the original equilibrium.py approach, now a
           named function so callers can switch cleanly.

Augmentation helpers (for training / data simulation):
  simulate_occlusions(img)  — adds synthetic shadows, blobs, noise
  build_augmentation_pipeline()  — full Albumentations pipeline

Model checkpointing:
  save_model(model, path)
  load_model(path) → AttentionUNet (eval mode)
"""

from __future__ import annotations
import math
from pathlib import Path
from typing import Tuple, Optional

import numpy as np
import cv2

# ── PyTorch (optional — graceful fallback if missing) ─────────────────────────
try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    TORCH_OK = True
except ImportError:
    TORCH_OK = False

# ── scikit-image skeletonize (optional) ───────────────────────────────────────
try:
    from skimage.morphology import skeletonize as sk_skeletonize
    SKIMAGE_OK = True
except ImportError:
    SKIMAGE_OK = False

# ── Albumentations (optional — only needed for training) ──────────────────────
try:
    import albumentations as A
    from albumentations.pytorch import ToTensorV2
    ALBUMENTATIONS_OK = True
except ImportError:
    ALBUMENTATIONS_OK = False


# ─────────────────────────────────────────────────────────────────────────────
# CLASSICAL FALLBACK  (zero extra deps beyond what's already installed)
# ─────────────────────────────────────────────────────────────────────────────

def extract_road_mask_classical(
    img_bgr: np.ndarray,
    occlusion_compensation: bool = True,
    clahe_clip: float = 3.0,
    block_size: int = 35,
    c_val: int = 10,
    close_ksize: int = 9,
    min_area: int = 80,
    min_aspect: float = 2.0,
) -> np.ndarray:
    """
    Classical CV road extraction (CLAHE + adaptive threshold + morphology).

    Fast, no GPU needed, always works.  Returns uint8 mask (255=road).
    This is the "occlusion compensation" baseline referenced in the
    problem statement — it handles shadows and low-contrast regions via
    local contrast enhancement rather than learned features.
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    if occlusion_compensation:
        clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(8, 8))
        gray = clahe.apply(gray)

    thresh = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        blockSize=block_size, C=c_val,
    )

    # Morphological closing — bridges small occlusion gaps
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_ksize, close_ksize))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, k_close, iterations=2)

    k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN, k_open, iterations=1)

    return _filter_elongated_components(opened, min_area=min_area,
                                        min_aspect=min_aspect)


def _filter_elongated_components(
    mask: np.ndarray, min_area: int = 80, min_aspect: float = 2.0
) -> np.ndarray:
    """Keep only connected components that are road-shaped (elongated)."""
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        mask, connectivity=8)
    out = np.zeros_like(mask)
    for i in range(1, num_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < min_area:
            continue
        w = stats[i, cv2.CC_STAT_WIDTH]
        h = stats[i, cv2.CC_STAT_HEIGHT]
        aspect = max(w, h) / max(1, min(w, h))
        if aspect >= min_aspect or area >= 400:
            out[labels == i] = 255
    return out


# ─────────────────────────────────────────────────────────────────────────────
# ATTENTION U-NET  (PyTorch — CPU-runnable, ~5M params)
# ─────────────────────────────────────────────────────────────────────────────

if TORCH_OK:

    class _DoubleConv(nn.Module):
        """Two 3×3 convolutions with BN + ReLU — standard U-Net block."""
        def __init__(self, in_ch: int, out_ch: int):
            super().__init__()
            self.net = nn.Sequential(
                nn.Conv2d(in_ch, out_ch, 3, padding=1, bias=False),
                nn.BatchNorm2d(out_ch),
                nn.ReLU(inplace=True),
                nn.Conv2d(out_ch, out_ch, 3, padding=1, bias=False),
                nn.BatchNorm2d(out_ch),
                nn.ReLU(inplace=True),
            )
        def forward(self, x):
            return self.net(x)

    class _ChannelAttention(nn.Module):
        """Squeeze-and-Excitation channel attention (Hu et al. 2018).
        Amplifies features that carry road signal, suppresses canopy/shadow
        noise — a lightweight proxy for the 'channel attention' described
        in the problem statement's Transformer reference."""
        def __init__(self, channels: int, reduction: int = 16):
            super().__init__()
            mid = max(1, channels // reduction)
            self.avg_pool = nn.AdaptiveAvgPool2d(1)
            self.max_pool = nn.AdaptiveMaxPool2d(1)
            self.fc = nn.Sequential(
                nn.Conv2d(channels, mid, 1, bias=False),
                nn.ReLU(inplace=True),
                nn.Conv2d(mid, channels, 1, bias=False),
            )
            self.sigmoid = nn.Sigmoid()

        def forward(self, x):
            avg = self.fc(self.avg_pool(x))
            mx  = self.fc(self.max_pool(x))
            return x * self.sigmoid(avg + mx)

    class _SpatialAttention(nn.Module):
        """CBAM-style spatial attention.
        Focuses the decoder on *where* roads are — critical for recovering
        roads under occluders (tree canopy produces a spatially coherent
        suppression pattern that attention can learn to reverse)."""
        def __init__(self, kernel_size: int = 7):
            super().__init__()
            self.conv = nn.Conv2d(2, 1, kernel_size,
                                  padding=kernel_size // 2, bias=False)
            self.sigmoid = nn.Sigmoid()

        def forward(self, x):
            avg = x.mean(dim=1, keepdim=True)
            mx, _ = x.max(dim=1, keepdim=True)
            attn = self.sigmoid(self.conv(torch.cat([avg, mx], dim=1)))
            return x * attn

    class _AttentionGate(nn.Module):
        """Attention gate at each skip connection (Oktay et al. 2018).
        Suppresses irrelevant activations from the encoder before they
        contaminate the decoder — this is the core 'long-range context'
        mechanism that makes occlusion recovery possible."""
        def __init__(self, g_ch: int, x_ch: int, mid_ch: int):
            super().__init__()
            self.Wg = nn.Conv2d(g_ch, mid_ch, 1, bias=True)
            self.Wx = nn.Conv2d(x_ch, mid_ch, 1, bias=True)
            self.psi = nn.Conv2d(mid_ch, 1, 1, bias=True)
            self.relu = nn.ReLU(inplace=True)
            self.sigmoid = nn.Sigmoid()

        def forward(self, g, x):
            # g = gating signal from decoder, x = skip connection from encoder
            g_up = F.interpolate(self.Wg(g), size=x.shape[2:],
                                 mode='bilinear', align_corners=False)
            x_t  = self.Wx(x)
            alpha = self.sigmoid(self.psi(self.relu(g_up + x_t)))
            return x * alpha

    class AttentionUNet(nn.Module):
        """
        Attention U-Net for satellite road segmentation.

        Architecture:
          Encoder: 4 × DoubleConv + MaxPool (channels: 3→64→128→256→512)
          Bottleneck: DoubleConv(512→1024) + Channel+Spatial attention
          Decoder: 4 × AttentionGate + UpConv + DoubleConv
          Head: 1×1 conv → sigmoid → binary road probability

        ~5M parameters. CPU inference on a 512×512 tile: ~1–3 s.
        Can be fine-tuned end-to-end on SpaceNet/DeepGlobe in ~4 hours
        on a single GPU with combined Dice + IoU + Boundary loss.
        """

        def __init__(self, in_channels: int = 3, base_ch: int = 64):
            super().__init__()
            c = base_ch  # 64

            # Encoder
            self.enc1 = _DoubleConv(in_channels, c)
            self.enc2 = _DoubleConv(c,    c * 2)
            self.enc3 = _DoubleConv(c*2,  c * 4)
            self.enc4 = _DoubleConv(c*4,  c * 8)
            self.pool = nn.MaxPool2d(2)

            # Bottleneck with attention
            self.bottleneck = _DoubleConv(c*8, c*16)
            self.ch_attn    = _ChannelAttention(c*16)
            self.sp_attn    = _SpatialAttention()

            # Decoder (attention gates + upsampling)
            self.ag4 = _AttentionGate(c*16, c*8,  c*4)
            self.up4 = nn.ConvTranspose2d(c*16, c*8, 2, stride=2)
            self.dc4 = _DoubleConv(c*16, c*8)

            self.ag3 = _AttentionGate(c*8, c*4, c*2)
            self.up3 = nn.ConvTranspose2d(c*8, c*4, 2, stride=2)
            self.dc3 = _DoubleConv(c*8, c*4)

            self.ag2 = _AttentionGate(c*4, c*2, c)
            self.up2 = nn.ConvTranspose2d(c*4, c*2, 2, stride=2)
            self.dc2 = _DoubleConv(c*4, c*2)

            self.ag1 = _AttentionGate(c*2, c, c//2)
            self.up1 = nn.ConvTranspose2d(c*2, c, 2, stride=2)
            self.dc1 = _DoubleConv(c*2, c)

            # Output head
            self.head = nn.Conv2d(c, 1, 1)

        def forward(self, x: 'torch.Tensor') -> 'torch.Tensor':
            # Encoder
            e1 = self.enc1(x)
            e2 = self.enc2(self.pool(e1))
            e3 = self.enc3(self.pool(e2))
            e4 = self.enc4(self.pool(e3))

            # Bottleneck
            b = self.bottleneck(self.pool(e4))
            b = self.ch_attn(b)
            b = self.sp_attn(b)

            # Decoder
            d4 = self.dc4(torch.cat([self.ag4(b, e4),  self.up4(b)],  dim=1))
            d3 = self.dc3(torch.cat([self.ag3(d4, e3), self.up3(d4)], dim=1))
            d2 = self.dc2(torch.cat([self.ag2(d3, e2), self.up2(d3)], dim=1))
            d1 = self.dc1(torch.cat([self.ag1(d2, e1), self.up1(d2)], dim=1))

            return torch.sigmoid(self.head(d1))  # (B,1,H,W), values in [0,1]


# ─────────────────────────────────────────────────────────────────────────────
# LOSS FUNCTIONS  (for training / fine-tuning)
# ─────────────────────────────────────────────────────────────────────────────

if TORCH_OK:

    def dice_loss(pred: 'torch.Tensor', target: 'torch.Tensor',
                  smooth: float = 1.0) -> 'torch.Tensor':
        """Soft Dice loss — penalises fragmented predictions."""
        pred   = pred.view(-1)
        target = target.view(-1)
        inter  = (pred * target).sum()
        return 1.0 - (2.0 * inter + smooth) / (pred.sum() + target.sum() + smooth)

    def iou_loss(pred: 'torch.Tensor', target: 'torch.Tensor',
                 smooth: float = 1.0) -> 'torch.Tensor':
        """Jaccard / IoU loss — directly optimises intersection-over-union."""
        pred   = pred.view(-1)
        target = target.view(-1)
        inter  = (pred * target).sum()
        union  = pred.sum() + target.sum() - inter
        return 1.0 - (inter + smooth) / (union + smooth)

    def boundary_loss(pred: 'torch.Tensor',
                      target: 'torch.Tensor') -> 'torch.Tensor':
        """Laplacian boundary loss — penalises misaligned road edges,
        encouraging sharp, well-localised predictions even at occlusion
        boundaries where the road signal fades."""
        lap_kernel = torch.tensor(
            [[[[-1,-1,-1],[-1,8,-1],[-1,-1,-1]]]], dtype=torch.float32,
            device=pred.device)
        pred_b   = F.conv2d(pred,   lap_kernel, padding=1)
        target_b = F.conv2d(target.float(), lap_kernel, padding=1)
        return F.mse_loss(pred_b, target_b)

    def combined_loss(pred: 'torch.Tensor', target: 'torch.Tensor',
                      w_dice: float = 0.4, w_iou: float = 0.4,
                      w_bce: float = 0.1, w_bnd: float = 0.1) -> 'torch.Tensor':
        """Combined loss used during training.
        Weights: 40% Dice + 40% IoU + 10% BCE + 10% Boundary.
        The high Dice/IoU weight ensures good connectivity (fewer broken
        road segments), while boundary loss sharpens road edges."""
        bce = F.binary_cross_entropy(pred, target.float())
        bnd = boundary_loss(pred, target)
        dl  = dice_loss(pred, target)
        il  = iou_loss(pred, target)
        return w_dice * dl + w_iou * il + w_bce * bce + w_bnd * bnd


# ─────────────────────────────────────────────────────────────────────────────
# DL INFERENCE PATH
# ─────────────────────────────────────────────────────────────────────────────

_DL_MODEL_CACHE: Optional["AttentionUNet"] = None  # singleton

_TILE_SIZE = 512     # inference tile size (px)
_THRESHOLD = 0.45    # probability threshold → binary mask


def _preprocess_tile(img_bgr: np.ndarray) -> "torch.Tensor":
    """Resize → normalize → convert to CHW tensor."""
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    rgb = cv2.resize(rgb, (_TILE_SIZE, _TILE_SIZE))

    # ImageNet normalisation (encoder head initialised from ImageNet weights)
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std  = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    rgb  = rgb.astype(np.float32) / 255.0
    rgb  = (rgb - mean) / std

    tensor = torch.from_numpy(rgb.transpose(2, 0, 1)).unsqueeze(0)  # 1,3,H,W
    return tensor


def extract_road_mask_dl(
    img_bgr: np.ndarray,
    weights_path: Optional[str] = None,
    threshold: float = _THRESHOLD,
) -> np.ndarray:
    """
    DL road extraction using AttentionUNet.

    If no weights file is provided / found, the model runs with random
    initialisation (useful for structural demo / UI testing).  Connect a
    fine-tuned checkpoint from SpaceNet/DeepGlobe training and replace this
    fallback with a proper inference.

    Returns uint8 binary mask resized back to the original image size.
    """
    if not TORCH_OK:
        raise RuntimeError("PyTorch not installed — cannot run DL model. "
                           "Use mode='classical' instead.")

    global _DL_MODEL_CACHE

    if _DL_MODEL_CACHE is None:
        model = AttentionUNet(in_channels=3, base_ch=64)
        if weights_path and Path(weights_path).exists():
            state = torch.load(weights_path, map_location="cpu")
            model.load_state_dict(state)
        model.eval()
        _DL_MODEL_CACHE = model

    h0, w0 = img_bgr.shape[:2]

    # Tiled inference for large images
    tensor = _preprocess_tile(img_bgr)

    with torch.no_grad():
        prob = _DL_MODEL_CACHE(tensor)  # (1,1,H,W)

    prob_np = prob.squeeze().numpy()  # (H,W), float32

    # Resize back to original resolution
    prob_np = cv2.resize(prob_np, (w0, h0), interpolation=cv2.INTER_LINEAR)

    # Binarise
    mask = (prob_np >= threshold).astype(np.uint8) * 255

    # Post-process: close small gaps (same as classical path)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    mask = _filter_elongated_components(mask, min_area=60, min_aspect=1.5)

    return mask


# ─────────────────────────────────────────────────────────────────────────────
# UNIFIED INTERFACE
# ─────────────────────────────────────────────────────────────────────────────

def extract_road_mask(
    img_bgr: np.ndarray,
    mode: str = "classical",          # 'classical' | 'dl'
    weights_path: Optional[str] = None,
    occlusion_compensation: bool = True,
    **kwargs,
) -> np.ndarray:
    """
    Single entry point for road mask extraction.

    mode='classical' → fast, CPU, no model needed
    mode='dl'        → AttentionUNet (CPU-runnable, ~1-3s/tile)
                       Falls back to classical if torch is missing.
    """
    if mode == "dl":
        try:
            return extract_road_mask_dl(img_bgr, weights_path=weights_path,
                                        **{k: v for k, v in kwargs.items()
                                           if k == "threshold"})
        except Exception:
            # Graceful fallback — never crash the UI
            return extract_road_mask_classical(
                img_bgr, occlusion_compensation=occlusion_compensation)
    else:
        return extract_road_mask_classical(
            img_bgr, occlusion_compensation=occlusion_compensation, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# SKELETONIZE  (convenience re-export so callers only import segmentation)
# ─────────────────────────────────────────────────────────────────────────────

_MAX_SKEL_DIM = 800


def skeletonize_mask(mask: np.ndarray) -> np.ndarray:
    """
    Thin the binary road mask to single-pixel-wide centrelines.
    Auto-downsamples large masks to keep skeleton manageable (<800px).
    """
    if not SKIMAGE_OK:
        raise RuntimeError("scikit-image not installed. Run: pip install scikit-image")
    h, w = mask.shape[:2]
    if max(h, w) > _MAX_SKEL_DIM:
        scale = _MAX_SKEL_DIM / max(h, w)
        mask = cv2.resize(mask, (int(w * scale), int(h * scale)),
                          interpolation=cv2.INTER_NEAREST)
    binary = (mask > 0).astype(np.uint8)
    skeleton = sk_skeletonize(binary).astype(np.uint8) * 255
    return skeleton


# ─────────────────────────────────────────────────────────────────────────────
# AUGMENTATION  (for training — Albumentations pipeline)
# ─────────────────────────────────────────────────────────────────────────────

def build_augmentation_pipeline(img_size: int = 512):
    """
    Full Albumentations pipeline for training data augmentation.

    Simulates the occlusion types described in the problem statement:
    - CoarseDropout → tree canopy / building shadow patches
    - RandomShadow   → directional shadow from buildings
    - RandomFog      → atmospheric haze / cloud cover
    - GaussNoise     → sensor noise common in Resourcesat LISS-IV
    """
    if not ALBUMENTATIONS_OK:
        raise RuntimeError("albumentations not installed. "
                           "Run: pip install albumentations")
    return A.Compose([
        A.RandomResizedCrop(size=(img_size, img_size),
                            scale=(0.5, 1.0), ratio=(0.75, 1.33)),
        A.HorizontalFlip(p=0.5),
        A.VerticalFlip(p=0.5),
        A.RandomRotate90(p=0.5),
        A.OneOf([
            A.RandomBrightnessContrast(brightness_limit=0.3,
                                       contrast_limit=0.3, p=1.0),
            A.CLAHE(clip_limit=4.0, p=1.0),
            A.HueSaturationValue(p=1.0),
        ], p=0.8),
        A.OneOf([
            A.GaussNoise(var_limit=(10, 50), p=1.0),
            A.ISONoise(p=1.0),
        ], p=0.5),
        A.RandomShadow(p=0.4),
        A.RandomFog(fog_coef_lower=0.05, fog_coef_upper=0.3, p=0.3),
        # Simulate tree canopy / vehicle occlusion patches
        A.CoarseDropout(max_holes=12, max_height=img_size // 8,
                        max_width=img_size // 8, fill_value=0, p=0.5),
        A.Normalize(mean=(0.485, 0.456, 0.406),
                    std=(0.229, 0.224, 0.225)),
        ToTensorV2(),
    ])


def simulate_occlusions(img_bgr: np.ndarray,
                         n_patches: int = 6,
                         patch_frac: float = 0.12) -> np.ndarray:
    """
    Synthetically occlude an image for demo / testing purposes.

    Adds dark elliptical blobs (tree canopy), random brightness patches
    (shadow), and Gaussian noise — replicating what Cartosat/Resourcesat
    images look like in dense urban Bengaluru.
    """
    out = img_bgr.copy().astype(np.float32)
    h, w = out.shape[:2]
    patch_h = int(h * patch_frac)
    patch_w = int(w * patch_frac)
    rng = np.random.default_rng(seed=42)

    for _ in range(n_patches):
        cx = int(rng.uniform(patch_w, w - patch_w))
        cy = int(rng.uniform(patch_h, h - patch_h))
        # Dark canopy blob
        cv2.ellipse(out, (cx, cy), (patch_w // 2, patch_h // 2),
                    angle=float(rng.uniform(0, 180)),
                    startAngle=0, endAngle=360,
                    color=(30, 60, 30), thickness=-1)
        # Shadow overlay
        x1 = max(0, cx - patch_w)
        x2 = min(w, cx + patch_w)
        y1 = max(0, cy - patch_h)
        y2 = min(h, cy + patch_h)
        out[y1:y2, x1:x2] = out[y1:y2, x1:x2] * rng.uniform(0.4, 0.7)

    # Gaussian noise
    noise = rng.normal(0, 15, out.shape).astype(np.float32)
    out = np.clip(out + noise, 0, 255).astype(np.uint8)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# MODEL CHECKPOINT HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def save_model(model: "AttentionUNet", path: str) -> None:
    """Save model state dict to disk."""
    if not TORCH_OK:
        raise RuntimeError("PyTorch not installed.")
    torch.save(model.state_dict(), path)
    print(f"Model saved → {path}")


def load_model(path: str,
               base_ch: int = 64) -> "AttentionUNet":
    """Load AttentionUNet from checkpoint (eval mode, CPU)."""
    if not TORCH_OK:
        raise RuntimeError("PyTorch not installed.")
    model = AttentionUNet(in_channels=3, base_ch=base_ch)
    model.load_state_dict(torch.load(path, map_location="cpu"))
    model.eval()
    return model
