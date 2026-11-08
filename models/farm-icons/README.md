# Farm Icons Model

The current runtime expects `best.onnx` in this directory. Weights and datasets are distributed separately and are not committed to Git.

Class order: `watering`, `harvest`, `fishing`, `midwifery`.

`labels.txt` defines the class order. `model.yaml` is an X-AnyLabeling configuration using `model_path: best.onnx` relative to this directory.

Keep the original training configuration, validation results and SHA-256 alongside separately distributed weights. Do not silently substitute a five-class model.
