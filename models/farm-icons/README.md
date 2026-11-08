# Farm Icons Model

This directory includes the current four-class YOLO11n farm model. Training images and annotations are not provided.

- `best.onnx`: runtime inference model, input size 960 × 960.
- `best.pt`: training checkpoint for further fine-tuning or export.
- `SHA256SUMS`: checksums for both weight files.

Class order: `watering`, `harvest`, `fishing`, `midwifery`.

`labels.txt` defines the class order. `model.yaml` is an X-AnyLabeling configuration using `model_path: best.onnx` relative to this directory.

## Provenance and limitations

These weights are from `gpu_wsl_batch003_night_local_e50`, exported on 2026-07-08, using Ultralytics 8.4.90. They are the same weights used by the original BatterFarm runtime. The weights are provided under AGPL-3.0; see the repository license and third-party notices.

The model is specific to the farm UI and reference capture conditions. No claim is made about accuracy on new game versions or unseen recording sessions. Validate detection before allowing live inputs. Do not silently substitute a five-class model, whose class IDs differ.
