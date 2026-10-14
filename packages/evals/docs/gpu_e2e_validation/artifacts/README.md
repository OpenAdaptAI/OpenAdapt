# Validation Artifacts

Raw terminal output and version data captured during the GPU E2E validation
session on 2026-03-04.

## Files

| File                          | Description                                           |
|-------------------------------|-------------------------------------------------------|
| `e2e_test_output.txt`         | Consolidated output from each validation stage: GPU detection, conda setup, package installation, and connectivity tests |
| `gpu_vm_stack_versions.txt`   | Full package version listing from the `verl-agent` conda environment on the GPU VM |
| `vagen_registry_output.txt`   | Output from installing VAGEN and registering the WAA environment in the VAGEN registry |

## Source

All outputs were captured from SSH sessions to the GPU VM
(`verl-train-00`, g5.xlarge, `3.236.121.184`) during the validation
described in the [main report](../README.md).
