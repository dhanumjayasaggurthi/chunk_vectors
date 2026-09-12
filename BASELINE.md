# Current implementation baseline

This branch preserves the uploaded `epod-chunk-vectors.zip` implementation before MIR-AI changes.

The exact original source snapshot is stored losslessly in `.bootstrap/part_00.b64` through `.bootstrap/part_08.b64` as a base64-encoded `tar.gz`, because the connected GitHub contents API cannot upload a local directory in one operation. Reconstruct it with:

```bash
cat .bootstrap/part_*.b64 | tr -d '\n' | base64 -d > epod-baseline.tar.gz
mkdir epod-baseline
tar -xzf epod-baseline.tar.gz -C epod-baseline
```

No `config.ini`, credentials, logs, IDE metadata, or generated cache files are included. The visible source files added alongside the snapshot are byte-for-byte from the same uploaded baseline.
