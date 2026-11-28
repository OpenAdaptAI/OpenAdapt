# Pixel-Only Grounding Escalation Bakeoff

Reproducible 4-arm harness comparing local pixel grounding against LAN and ZDR/BAA VLM escalation on pixel-only UI fixtures.

## Arms

1. **Arm A (`arm_a_local_template_dinov2`)**: Local OpenCV template matching plus `LocalGroundingModel` (DINOv2-small int8 ONNX) with strict count-never-pick look-alike abstention. Runs locally with zero network egress.
2. **Arm C (`arm_c_lan_remote_vlm`)**: On-prem LAN `RemoteGrounder` (`openadapt_flow.runtime.remote_vlm`). Active when `OPENADAPT_VLM_URL` (or `OPENADAPT_FLOW_VLM_URL`) is set; otherwise reported as `skipped_no_credentials`.
3. **Arm D Raw (`arm_d_zdr_vlm_raw`)**: `ZdrVlmGrounder` (`openadapt_flow.runtime.zdr_vlm_grounder`) without visual masking, with `HealedAnchorCache`. Active when `OPENADAPT_ZDR_VLM_URL` is set; otherwise reported as `skipped_no_credentials`.
4. **Arm D Masked (`arm_d_zdr_vlm_masked`)**: `ZdrVlmGrounder` paired with `SyntheticTwinProjector` visual masking (`openadapt_flow.runtime.synthetic_twin_mask`) and `HealedAnchorCache`. Active when `OPENADAPT_ZDR_VLM_URL` is set; otherwise reported as `skipped_no_credentials`.

## Fixtures

Six pixel-only test cases rendered deterministically at native pixel resolution:

| Case ID | Category | What it tests |
| --- | --- | --- |
| `scaled_150_submit` | `scaled` | 150% display scaling and layout shift on a form with PHI header |
| `dark_theme_attach` | `themed` | Light-theme recorded toolbar icon replayed in dark theme |
| `blank_field_dob` | `blank_field` | Three identical blank text inputs disambiguated only by adjacent labels |
| `lookalike_patient_rows` | `look_alike` | Four identical `Open` buttons in a patient worklist (wrong row = `wrong_click`) |
| `choice_widget_select2_crop` | `choice_widgets_crop` | Pixel-cropped Select2 dropdown at 125% scale |
| `choice_widget_aria_listbox_crop` | `choice_widgets_crop` | Pixel-cropped ARIA listbox in dark theme |

## Scoreboard Metrics

Every measured arm reports:

- `wrong_clicks`: Hard safety gate (must be `0`).
- `resolved_rate`: Fraction of cases where the returned point landed inside the target box.
- `human_interventions_per_100`: Abstention rate scaled per 100 steps.
- `avg_latency_ms`: Mean per-case grounding latency in milliseconds.
- `cache_hit_rate`: Local `HealedAnchorCache` hit rate across the repeat pass.
- `status`: `measured` when executed against real local ONNX or a configured endpoint, or `skipped_no_credentials` when credentials/URL are absent. No scores are simulated or fabricated.

## Usage

From `packages/flow`:

```bash
uv run --extra dev python benchmark/pixel_escalation_bakeoff/run_bakeoff.py
```

To include Arm C (LAN appliance) and Arm D (ZDR/BAA endpoint):

```bash
OPENADAPT_VLM_URL=http://10.0.0.10:8080 \
OPENADAPT_ZDR_VLM_URL=https://zdr.example.internal/v1 \
OPENADAPT_ZDR_VLM_MODEL=gpt-4o \
OPENADAPT_ZDR_VLM_API_KEY=... \
uv run --extra dev python benchmark/pixel_escalation_bakeoff/run_bakeoff.py --output /tmp/bakeoff_scoreboard.json
```
