# Tool reliability contract: interpreting results

Use these rules when calling Konnect tools and interpreting their results during a
design workflow.

## Establish what happened

1. Bind the requested document and identify the source of each relevant result:
   live editor, saved file, or a stated mixture. Await state-changing prerequisites
   before dependent checks; issuing save and DRC concurrently does not order them.
2. Inspect the complete response, including errors, warnings, per-item results and
   coverage. A successful transport or `isError: false` alone does not establish
   that every requested item was applied or every required check ran.
3. Verify the actual outcome using readback and the relevant independent evidence
   (for example netlist, ERC/DRC, or rendered inspection). Compare with intent;
   echoing the request is not evidence that the operation applied it.
4. Classify incomplete work before continuing. If a response lacks outcome or
   source information, inspect through available tools or report that uncertainty.
   Use the fields the installed version actually provides; the categories below
   are meanings, not a guaranteed response schema.
5. Recover according to observed state. Retry only unapplied items of a partial
   batch. After a timeout or uncertain write, reload and inspect before deciding
   what to repeat. A refusal that proves nothing changed permits correcting the
   request and retrying. An error after a write does not prove rollback.

| Outcome | Required interpretation |
|---|---|
| Complete | The requested operation and relevant verification completed. |
| Partial | Identify applied, failed and unattempted items or checks; completion remains incomplete. |
| Refused/failed before mutation | State the cause and evidence that nothing was applied. |
| Uncertain after possible mutation | Preserve the target and diagnostic; inspect before retrying. |
| Check unavailable | Report missing evidence; do not substitute zero findings or a pass. |

Separate execution from engineering findings: a completed DRC run can report
violations, and a valid search can return no matches. Both differ from a check
that could not run or a result that could not be decoded. Required unavailable
evidence makes a design verdict incomplete.

## Known gaps in Konnect v0.13.0

Only `add_schematic_component`, `batch_place_components` and `run_design_review`
return the shared `outcome` field. Other tools keep older response shapes, so
apply the rules above to whatever fields they do return. Specific gaps:

| Area | Gap |
|---|---|
| DRC | Default calls read the saved file, not the live board. Save first and report the source. |
| Explicit config loading | A failed explicit config can fall back to defaults and still report success. Check `get_effective_config` sources. |
| `get_layer_list`, `get_netclasses` | Read the saved file even when other tools changed the live board. Save before trusting them. |
