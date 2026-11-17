# Pergola rejection diagnosis — November 17, 2026

Read-only inspection of the live media queue confirmed that the newest Instagram pergola job saved `nano_banana_2.1` and `veo3.1`. Six stills had been saved when inspected, with no completed video clips. Its failures occurred during scene review, before Veo animation. The earlier cancelled pergola job had no persisted model fields and therefore retained the legacy Gen-4 pair.

The original idea asked for people grilling and eating in a rainy backyard pergola. The reviewed shot described a wet backyard after a shower. Neither chose an exact post count or removed a center beam. The selected company construction image shows four corner posts and two louver bays separated by a supporting intermediate beam within one roof plane.

The generated draft added six posts. Scene planning then introduced an uninterrupted span, and retries strengthened this into perimeter-only framing. Several rejections treated a transverse beam as a defect even though the reference contained it. One saved attempt's only recorded rejection was that beam. Other attempts also had cropped framing or supports obscured by diners and furniture; those findings require separate assessment and were not dismissed.

## Change

The prompt writer and reference planner now preserve reference construction unless the actual request selects a supported change. Dimensional examples in Builder guidance are configuration-specific. Scene and final visual review receive the original request separately from generated specifications. Previous diagnoses are checked against actual requests and product evidence before becoming correction requirements.

Manual corrections retain before/after text. Only the edit is newly authored; unchanged generated boilerplate is not promoted into an instruction. Automatic suggested corrections are not recorded as operator edits. Legacy edits remain explicit when their baseline is unavailable. Existing paid plans, rejected scenes and task histories are preserved.

## Live planning replay

Two isolated calls used the production reference selector with the saved company image, original idea, reviewed shot, Builder rules and generated draft. The retry case also used the saved rejected image and automatic-retry findings from before manual edits. The candidate set was limited to the exact reference already selected by production; this was not a repeat of the full seven-candidate selection.

| Case | Observed result |
|---|---|
| Initial plan | Preserved four corner posts, the intermediate beam and two louver bays. Identified six posts as a generated-draft addition. |
| Automatic correction | Removed the unsupported six-post and uninterrupted-span requirements. Retained correction of genuinely obstructed support sightlines. |

Both calls completed successfully using `gpt-6-astra`. They exercised the actual planner API, not a mocked model response. This is narrow evidence that the instruction conflict is addressed on these inputs. No new Runway generation, animation, finished-video gate or pass-rate trial was performed. Production was inspected read-only; no server deployment or job mutation was performed.

## Regression validation

All 119 distinct tests across reference grounding, scene generation, scene/clip replacement, automatic retry, prompt writing, product rules and visual-review scope passed across the targeted runs. The two browser tests initially lacked BROWSER_BINARY; both passed after configuring the installed Chrome executable for the test process. The final automatic replacement test also passed with assertions that generated suggestions do not become operator edits and review findings remain present. Ruff and git diff --check passed. The two replay prompts composed to 713 and 726 UTF-16 units, within the 1,000-unit limit without truncation.
