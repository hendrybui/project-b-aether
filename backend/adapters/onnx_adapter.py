"""Convert HTDemucs 6s to ONNX and apply INT8 quantization for faster CPU inference.

Usage:
    python convert_to_onnx.py              # Export ONNX + INT8 quantized
    python convert_to_onnx.py --fp32-only  # Export FP32 ONNX only (no quantization)

Outputs:
    models/htdemucs_6s.onnx        - FP32 ONNX model (~110MB)
    models/htdemucs_6s_int8.onnx   - INT8 quantized model (~28MB)
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import torch

MODELS_DIR = Path(__file__).parent

# Segment length for chunked processing (seconds * 44100 Hz)
# HTDemucs uses segment=39/5=7.8s internally. We must match for ONNX export.
CHUNK_SAMPLES = int(44100 * 39 / 5)  # 343980


def export_onnx(model, output_path: Path, chunk_samples: int = CHUNK_SAMPLES):
    """Export a single HTDemucs model to ONNX using TorchDynamo (handles dynamic shapes)."""
    model.eval()
    device = next(model.parameters()).device

    # Dummy input: stereo audio chunk
    dummy = torch.randn(1, 2, chunk_samples, device=device)

    print(f"Exporting ONNX (dynamo) to {output_path} ...")
    print(f"  Input shape: {tuple(dummy.shape)}")

    try:
        # Use dynamo-based export which handles dynamic shapes properly
        import torch.onnx as _tonnx
        onnx_program = _tonnx.dynamo_export(
            model,
            dummy,
            export_options=torch.onnx.ExportOptions(dynamic_shapes=True),
        )
        onnx_program.save(str(output_path))
    except Exception as e:
        print(f"  Dynamo export failed: {e}")
        print("  Trying legacy export with dynamic axes ...")
        torch.onnx.export(
            model,
            dummy,
            str(output_path),
            input_names=["mix"],
            output_names=["stems"],
            dynamic_axes={
                "mix": {0: "batch", 2: "samples"},
                "stems": {0: "batch", 2: "samples"},
            },
            opset_version=17,
            do_constant_folding=True,
        )

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  Exported: {size_mb:.1f} MB")
    return output_path


def quantize_int8(onnx_path: Path, output_path: Path):
    """Apply INT8 quantization to an ONNX model."""
    try:
        from onnxruntime.quantization import (
            QuantFormat,
            QuantType,
            quantize_dynamic,
        )
    except ImportError:
        print("ERROR: onnxruntime not installed. Run: pip install onnxruntime")
        sys.exit(1)

    print(f"Quantizing INT8 -> {output_path} ...")
    quantize_dynamic(
        str(onnx_path),
        str(output_path),
        weight_type=QuantType.QInt8,
    )

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"  Quantized: {size_mb:.1f} MB")


def verify_onnx(onnx_path: Path, model, chunk_samples: int = CHUNK_SAMPLES):
    """Quick sanity check: run both PyTorch and ONNX, compare outputs."""
    import onnxruntime as ort

    print(f"Verifying {onnx_path.name} ...")
    model.eval()
    device = next(model.parameters()).device

    dummy = torch.randn(1, 2, chunk_samples, device=device)

    # PyTorch output
    with torch.no_grad():
        pt_out = model(dummy).cpu().numpy()

    # ONNX output
    sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    ort_out = sess.run(None, {"mix": dummy.cpu().numpy()})[0]

    diff = np.abs(pt_out - ort_out).mean()
    print(f"  Mean abs difference (PyTorch vs ONNX): {diff:.6f}")
    if diff < 0.01:
        print("  Verification PASSED")
    else:
        print(f"  WARNING: difference is high ({diff:.4f}), quality may be affected")


def main():
    parser = argparse.ArgumentParser(description="Convert HTDemucs to ONNX")
    parser.add_argument("--fp32-only", action="store_true", help="Skip INT8 quantization")
    parser.add_argument("--no-verify", action="store_true", help="Skip verification")
    parser.add_argument("--chunk-seconds", type=float, default=10.0, help="Chunk length in seconds")
    args = parser.parse_args()

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    chunk_samples = int(args.chunk_seconds * 44100)

    print("Loading HTDemucs 6s model ...")
    from demucs.pretrained import get_model
    bag = get_model("htdemucs_6s")
    # BagOfModels wraps a single HTDemucs; extract it
    inner = bag.models[0]
    inner.eval()

    # Disable use_train_segment so the model uses dynamic lengths
    # instead of a hardcoded training_length reshape.
    # This makes the ONNX export work with any input length.
    inner.use_train_segment = False

    fp32_path = MODELS_DIR / "htdemucs_6s.onnx"
    export_onnx(inner, fp32_path, chunk_samples)

    if not args.no_verify:
        verify_onnx(fp32_path, inner, chunk_samples)

    if not args.fp32_only:
        int8_path = MODELS_DIR / "htdemucs_6s_int8.onnx"
        quantize_int8(fp32_path, int8_path)

        if not args.no_verify:
            verify_onnx(int8_path, inner, chunk_samples)

    print("\nDone! Models saved to:", MODELS_DIR)


if __name__ == "__main__":
    main()
