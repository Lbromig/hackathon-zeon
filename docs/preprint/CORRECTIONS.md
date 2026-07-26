# Adversarial review: outstanding corrections

An adversarial fact-check was run against the repository and against public
datasheets. It returned **30 must-fix items and 12 should-fix items**. Applied so far:

- **Item 1** the per-pixel noise conversion. A datasheet percentage was treated as
  scale-invariant. The two bounds are anchored to different distances (0.5 m and 2 m)
  and stereo error grows as the square of range, so the figures were not comparable.
  Propagated correctly, **the device ranking inverts.** The old text overstated the
  competing device's noise by roughly eight times and then used the overstatement to
  justify the device already chosen.
- **Item 2** `6.553 m` was a 16-bit saturation sentinel (65535 counts at 1e-4 m),
  not a range reading, and `0.093 m` was below the minimum range quoted two sections
  later. "Valid pixels" meant "nonzero pixels", which undermines the valid-fraction
  refusal gate.
- **Item 4** a sentence asserting the closure measurement happened later, next to one
  saying it never happened.
- **Item 5** a fix claimed in the present tense that is not on the audited branch.
- **Item 8** "non-deterministic" described a fault that was four-for-four under a
  fixed condition. It is condition-dependent. Also corrected in `FINDINGS.md`.
- **Item 13** the two-serial finding now excludes the confound this session's own
  attribution bug would otherwise create, and the abstract no longer generalises one
  observation to a device class. Also corrected in `FINDINGS.md`.
- **Item 20** "raises SIGSEGV" to "terminates on SIGSEGV", and the build is named.
  A fault is delivered to a process, not raised, and this paper's second contribution
  is that exact distinction. Also corrected in `FINDINGS.md`.

**Everything below is still outstanding.** Several are structural rather than
wording: the taxonomy is never validated (item 7), no false-refusal rate is reported
for a fail-closed system (item 32), and item 41 argues that a paper announcing its own
honesty converts every unfixed gap into a broken promise. Item 42 flags one review
request that is not an author error and should not be "fixed".

Note also item 23: the claim in `README.md` that everything was observed on hardware
is false for most of the evidence blocks, and the additions made in this pass did not
change that.

---

The file reviewed as `SUPPLEMENT-methods-and-mitigations.md` no longer exists at that path. Its text is now `/Users/DiLoaner/Downloads/preprint/SUPPLEMENT-methods-and-mitigations.md` (716 lines, prefaced by a status block; content otherwise unchanged, line numbers shifted +30). All line numbers below refer to that file. Two review items are already fixed there and are omitted: the 6 fusion tests are now excluded from the totals (496-502), and the 173-vs-184 delta is now explained (518-521).

Because that file's own header says its Sections 1, 2, 7 and 8 duplicate `DRAFT.md`, every fix below touching the Abstract or Sections 1, 2, 7, 8 must also be applied in `/Users/DiLoaner/Downloads/preprint/DRAFT.md`. Items 8 and 20 also apply to `/Users/DiLoaner/Downloads/preprint/FINDINGS.md`.

---

# MUST FIX (factually wrong or overclaiming)

## 1. Section 4.4, lines 536-541 - the per-pixel noise conversion is invalid and inverts the paper's own conclusion

Current: "Close-range noise also favours the short-range device, with root-mean-square spatial noise specified at 1 percent against 2 percent, corresponding to per-pixel standard deviations of approximately 2.5 mm and 5.0 mm at 250 mm. A 15 mm step is therefore approximately 6 standard deviations per pixel on the short-range device and 3 on the general-purpose one, before spatial averaging over the cap face."

Why wrong: the two datasheet bounds (Table 4-14, doc 337029-017) are anchored to different distances - D401/D405 <=1% at <=0.5 m, D430/D435/D435i <=2% at <=2 m - so "1 percent against 2 percent" is not a like-for-like comparison. Stereo depth error grows as Z^2 (eps = delta*Z^2/(b*f)), so the percentage is not scale-invariant and cannot be multiplied by 250 mm. Propagating each bound to 250 mm through each device's own baseline and focal length gives ~1.25 mm (D405) and ~0.63 mm (D435): the ranking inverts, because the D435's b*f product is ~2.6x the D405's. The paper overstates D435 noise by roughly 8x and then uses the overstatement to justify the device it recommends. Additionally the <=2% bound is specified at HD, where D435 min-Z is 280 mm, so the 5.0 mm figure describes a configuration the paper states two sentences earlier produces no depth at all.

Replace with: "Close-range feasibility is determined by minimum range rather than by noise. The family datasheet bounds root-mean-square spatial noise at 1 percent of range for the short-range device (specified at up to 0.5 m) and 2 percent for the general-purpose devices (specified at up to 2 m); the two bounds are anchored to different distances and stereo depth error scales as the square of range, so neither may be rescaled linearly to a 250 mm standoff and the two figures are not directly comparable. Propagating the short-range bound to 250 mm under a Z^2 model gives a per-pixel standard deviation of at most approximately 1.25 mm, so a 15 mm step is at least 8 standard deviations per pixel even after the factor of root-two penalty incurred by measuring a difference against a reference. We do not quote a per-pixel figure for the general-purpose devices at 1280x720, because at a 250 mm standoff that configuration returns no depth."

Also fix Section 6, line 611: "per-pixel noise approximately doubles" inherits the same error - replace with "and the per-pixel noise specification is anchored to a different range, so the thresholds require re-derivation rather than rescaling."

## 2. Section 4.1, lines 435-437 - "6.553 m" is a 16-bit saturation artifact, not a measurement

Current: "A representative frame set contained 734,469 valid depth pixels spanning 0.093 m to 6.553 m. A second acquisition returned 882,330 valid pixels over 0.109 m to 4.016 m."

Why wrong: 65535 counts x 1e-4 m/count = 6.5535 m exactly. The reported maximum is a saturated uint16 at the device's own depth scale, not a range reading; the D405's specified useful range ends at 0.5 m. 4.016 m (40160 counts) is likewise far outside the device's range and is invalid-region noise. The reported minimum, 0.093 m, is below the 0.100 m min-Z the same paper quotes for 1280x720 at line 530. Both endpoints are out of spec, so "valid" evidently means "nonzero" - which matters because Section 3.5's refusal condition (349-351) is a valid-pixel fraction, and a fraction computed over a nonzero predicate can be satisfied entirely by noise. Presenting a saturated sentinel as a depth span is exactly the confidently-wrong-value failure Section 5 is about.

Replace with: "A representative frame set contained 734,469 pixels with nonzero depth. Of these, the returned values extend to the 16-bit ceiling of the device's depth scale (65535 counts, 6.5535 m), which is a saturation sentinel rather than a range measurement, and down to 0.093 m, below the 0.100 m nominal minimum for this resolution. We therefore report nonzero-pixel counts, not valid-measurement counts: [N] pixels of the first frame set and [N] of the second fell inside the device's specified 0.07-0.50 m window. A second acquisition returned 882,330 nonzero pixels. The depth stream resolution was [state it]." Define the validity predicate used by the Section 3.5 gate in the same place, and state whether it range-gates.

## 3. Sections 4.1, 4.2 and 6 - "throughout" contradicts the frames the paper says it captured

Current, line 469: "The operating-system capture path was refused to the host process throughout." Line 440-443: "An MJPEG transport delivered 21 JPEG frames in four seconds over HTTP... A subsequent two-camera configuration delivered 30 frames from each of two devices." Lines 613-615: "All cameras were operated as generic capture devices, yielding no depth and no factory intrinsics."

Why wrong: operating a camera as a generic capture device is the operating-system capture path. Either it was not refused throughout, or the 21-frame and 30-frame results did not occur. As written the two subsections of Results contradict each other and the reader cannot tell which phase each number belongs to. The paper contains no dates and no phase labels anywhere.

Replace with: scope line 469 to its process and phase - "The operating-system capture path was refused to the FastAPI server process in the configuration described in Section 4.1; frames were obtained only from a separately launched process holding the entitlement." Then add a configuration table immediately before Section 4.1 with columns: phase, date, cameras attached, acquisition path (vendor SDK / OS capture), and what that phase demonstrated. Every number in Section 4.1 must cite a phase.

## 4. Section 4.4, lines 543-546 versus Section 6, line 601 - one of these sentences is false

Current, 543-546: "We did not validate this comparison experimentally. The short-range device was removed from the deployment before the closure measurement was performed on a real container, and was replaced by two general-purpose devices, which inverts the margin above." Current, 601: "The closure measurement has never been run on a physical container."

Why wrong: "removed before the measurement was performed" asserts the measurement was performed, just later. Section 6 says it never happened.

Replace 543-546 with: "We did not validate this comparison experimentally. The short-range device was removed from the deployment before any closure measurement could be attempted on a physical container, and was replaced by two general-purpose devices whose minimum range excludes the standoff used here. This is stated as a limitation in Section 6, not as a result."

## 5. Section 5, lines 579-581 - the fix is claimed in the present tense but is not on the audited code

Current: "Properties are now read from each device's own registry block, and are left absent when no block matches rather than borrowed from a neighbour."

Why wrong: verified against the repository at `HEAD` = `fix-cameras-api-segfault` @ `2220675`. The commit containing that fix (`f7532bd`) is reachable only from the unmerged branch `feat-cap-depth-channel`; `git merge-base --is-ancestor f7532bd HEAD` fails, and it is not on `main` either. On the audited tree, `camera_probe.py` still recomputes `start = raw.find("RealSense")` from the top of the dump inside the per-device loop, so every device still receives the first block's serial, and `claimed_by` is still filled by a whole-dump `if driver in raw` substring test. The bug the paper says is fixed is live. The paper correctly caveats the fusion tests as being on an unmerged branch (496) and applies no such caveat here.

Replace with: "Properties are read from each device's own registry block in a fix on a branch not merged at time of writing; on the mainline the borrowing behaviour is still present. Where no block matches, the fixed implementation leaves the property absent rather than borrowing from a neighbour." Alternatively, merge the fix before submission and cite the commit.

## 6. Abstract lines 44-46 and Section 3.1 lines 207-210 - the "exactly one operator action" invariant is violated by the table that demonstrates it

Current, 44-46: "a failure taxonomy in which every sensor acquisition failure resolves to exactly one named diagnosis carrying one operator action". Current, 208-210: "Each diagnosis carries exactly one operator action, and the remedy table is stored adjacent to the diagnosis enumeration..." Table, 217-222: `permission_denied` -> "Grant, or launch from a process that can be granted"; `exclusive_access` -> "Release the holder, or elevate"; `driver_claimed` -> "Use the OS capture path, or another host"; `opened_but_no_frames` -> "Contention, or an under-negotiated link".

Why wrong: four of seven rows carry two actions, and `opened_but_no_frames` carries two hypotheses rather than any action. "Every sensor acquisition failure" is an unproven completeness claim over seven rows - firmware/SDK version mismatch, thermal throttle, partial stream, unsupported resolution and corrupt frames are unmapped, and link speed, which Section 3.2 calls a first-class check (239-242), has no row at all.

Replace 44-46 with: "a failure taxonomy that resolves the acquisition failures we encountered to one of seven named diagnoses, each carrying a bounded remedy, distinguishing conditions that are routinely conflated". Replace 208-210 with: "Each diagnosis carries a remedy, and the remedy table is stored adjacent to the diagnosis enumeration so a diagnosis cannot be added without one. Four diagnoses admit two remedies because two distinguishable causes share one signature; we consider narrowing these a defect to be closed rather than a property of the domain. The taxonomy covers the conditions we encountered and is not claimed to be complete." Add a `link_underspeed` row, or state in the text that link rate is reported as an attribute rather than as a diagnosis.

## 7. Sections 1.3 and 4 - contributions 1 and 2 have no result; the taxonomy is never validated

Current, 134-138: "A preflight gate (Section 3.1) that must pass before any component may assert a physical observation... A failure taxonomy (Section 3.2) mapping each acquisition failure to one operator action".

Why wrong: Section 4 reports no exit-code observation, no distribution of which diagnoses fired, and no experiment that induces each of the seven conditions and confirms the correct diagnosis is emitted. The seven-row table is asserted, not measured. This is the cheapest experiment in the paper - unplug the cable, hold the device in a second process, deny the permission, force a USB 2 link - and its absence means the two first-listed contributions are undemonstrated.

Replace with: either add a Section 4 subsection "Preflight validation" with an induced-condition table (condition induced -> diagnosis emitted -> exit code -> correct?), or demote both bullets from contributions to design description: "A preflight gate (Section 3.1) and an accompanying failure taxonomy (Section 3.2), presented as a design; we report no experiment inducing each condition and confirming the diagnosis emitted."

## 8. Section 4.1, lines 445-447 - "non-deterministic" is the opposite of what the observation shows

Current: "Pipeline initialisation returned SIGSEGV (exit 139) on four consecutive attempts under one condition, and returned a clean `RuntimeError` for the identical call under another, establishing the fault as non-deterministic."

Why wrong: four-for-four under a fixed condition, plus a different outcome under a different condition, is evidence of condition-dependence - determinism with respect to unmodelled state. It is the opposite of the stated conclusion, and "the identical call" is not identical if the condition differs. Neither condition is named, so the claim cannot be evaluated. This also undercuts Section 3.3's causal story (258-260), which requires the fault to track claimability.

Replace with: "Pipeline initialisation returned SIGSEGV (exit 139) on four consecutive attempts with [condition A: state it], and returned a clean `RuntimeError` for the same call with [condition B: state it]. The fault is therefore condition-dependent rather than intermittent: it was reproducible under a fixed condition, and the condition that selects between a signal and an exception is [named / not yet identified]." Apply the same correction to `FINDINGS.md:270` ("The fault is non-deterministic").

## 9. Section 3.4, lines 304-310 - the sanctioned escape hatch is the artifact Section 1.1 condemns

Current: "the pass is labelled as simulated in its human-readable detail and its structured data; confidence remains zero so any confidence-weighted aggregation gives it no weight; and the switch cannot affect an implemented agent..."

Why wrong: the condemned stub at line 94 is `ok=True, confidence=0.0, detail="stub"` - already labelled, already confidence zero. The only material difference is a structured flag, and the paper's own narrative at line 98 establishes that the consumer gated on `result.ok` and ignored both confidence and detail. The mitigation depends on downstream honouring a signal the paper documents downstream as ignoring, for a hazard Section 1.1 calls "the primary hazard in verified autonomy".

Replace with: "the simulated result is not `ok=True`. It occupies a third state that the orchestrator's gate cannot read as a pass, is labelled simulated in both its detail string and its structured data, carries zero confidence, and cannot be applied to an implemented agent, since a mechanism able to force a pass on a real checker is a mechanism for fabricating evidence. A gate that consumes only `result.ok` therefore halts on a simulated result, which is the behaviour a demonstration must not be able to suppress." Add a test showing the gate rejecting it, or delete the escape hatch and state that demonstrations halt.

## 10. Abstract lines 41-43 versus Section 3.1 lines 204-205 - "may not" is asserted, "can" is delivered

Current, abstract: "a verification-gated architecture in which no downstream component may claim to have observed anything until a preflight has established that a sensor was attached, that the process could open it, and that frames were actually delivered." Current, 3.1: "The process exits zero only if frames were actually delivered. A run harness can therefore gate on the exit code."

Why wrong: nothing in the paper shows the gate wired into any downstream component and no result shows a component blocked by it. The abstract asserts an enforced invariant; the body offers an available mechanism. This is precisely the defect Section 7 names at 663-667 ("The intent was present and unenforced"), so a reviewer will quote the paper against itself.

Replace with: "a verification-gated architecture in which a preflight must establish that a sensor was attached, that the process could open it, and that frames were actually delivered, exposed as a process exit code suitable for machine gating by a run harness. We describe the gate and its diagnostics; we do not report a downstream component refusing to answer without it."

## 11. Section 2, lines 164-166 (and Section 3.6, line 364) - Kabsch is not the standard basis for hand-eye calibration

Current: "The Kabsch algorithm recovers the optimal rotation between two paired point sets under least squares, and is the standard basis for hand-eye calibration from paired observations."

Why wrong: the first clause is correct; the second is not. Hand-eye calibration in the standard literature is the AX=XB problem (Tsai and Lenz 1989; Horaud and Dornaika 1995; Daniilidis), none of which is Kabsch. What this work does is 3D-3D rigid point-set registration with known correspondences, whose canonical citations are Arun, Huang and Blostein (1987), Horn (1987) and Umeyama (1991). Kabsch also handles rotation only; translation comes from centroid alignment, which the paper elides. A robotics reviewer reads this as misuse of the term.

Replace with: "Rigid transform estimation. The Kabsch algorithm recovers the optimal rotation between two paired point sets under least squares, with translation obtained by centroid alignment (equivalently Arun et al. 1987; Horn 1987). Because our correspondences are known - the machine is driven to commanded coordinates and the end-effector marker is observed at each - we solve 3D-3D point-set registration directly rather than the AX=XB hand-eye formulation of Tsai and Lenz (1989)." Amend line 364 to "Camera-to-robot registration by rigid transform requires the observed feature's position in metres in the camera frame..."

## 12. Section 2, line 173 - "Structured-light" is the wrong sensing modality and contradicts the next sentence

Current heading: "**Structured-light and stereo RGB-D sensing.** The Intel RealSense D400 series provides stereo depth with an optional infrared projector."

Why wrong: the D400 series is active stereo, not structured light. The datasheet states it uses stereo vision to compute depth, with a projector that projects a static non-visible IR pattern to add texture; nothing is coded and decoded. The body text is correct and the heading contradicts it.

Replace with: "**Active-stereo RGB-D sensing.**" (body text unchanged).

## 13. Section 4.1, lines 462-463, and Abstract lines 60-61 - the two-serial finding is confounded by the paper's own bug, and the abstract pluralises n=1

Current, 4.1: "A single physical D405 reported serial `352122272054` through the SDK and `351623070085` through the USB descriptor." Current, abstract: "...and depth cameras that report two mutually inconsistent serial numbers depending on which layer is queried."

Why wrong: (a) Section 5 (576-581) documents the authors' own enumeration defect, which applies one device's property to every device of that vendor - a mechanism that fabricates exactly this kind of cross-source serial mismatch - and the finding is reported without excluding it. (b) "A single physical D405" is asserted, not established; the bus state at the time of the read is never stated. (c) Only `351623070085` is traceable to a committed artifact (`docs/camera-preflight.md:80`, attributed to the D405 via IOKit, 0x8086:0x0b5b); `352122272054` appears nowhere in the working tree or in `git log --all -S` on any branch. (d) The abstract generalises one observation to a class of devices.

Replace 4.1 with: "With one camera attached and confirmed as the only D400-series device on the bus, a physical D405 reported serial `352122272054` through the SDK and `351623070085` through the USB descriptor. This read was taken after the property-attribution defect of Section 5 was corrected, so the mismatch is not an artifact of that defect." Cite the bench record for `352122272054`. Replace the abstract clause with "and a depth camera that reported two mutually inconsistent serial numbers depending on which layer was queried."

## 14. Section 4.1, lines 434-443 - none of the acquisition figures are traceable, and a committed document contradicts them

Current: "Colour at 1280x720 and metric depth were acquired from an Intel RealSense D405... 734,469 valid depth pixels... 882,330 valid pixels... 9.9999997 x 10^-5 m per count... 21 JPEG frames in four seconds... 30 frames from each of two devices..." and line 458 "30 unprivileged eviction attempts across three rounds".

Why wrong: none of these figures appear anywhere in the repository or in any branch's history (`grep -rn` plus `git log --all -S`). The only committed statement about frames from that camera is `docs/camera-preflight.md:89`, under the heading "Hardware state, honestly": "No frame has been captured from this camera. Not one." That document is never updated after `2e113b1`, so it may simply be stale relative to a later bench session - but as the repository stands it directly contradicts the paper's central hardware result. (The depth scale itself is independently corroborated: a librealsense report shows a D405 printing 9.999999747378752e-05.)

Replace with: no wording change is sufficient. Either update `docs/camera-preflight.md` and commit the session log, frame counts and scale readout that these numbers come from, and cite that record in Section 4.1, or withdraw the acquisition paragraph. Do not submit with the repository asserting no frame was ever captured.

## 15. Section 5, line 553 - "Each was encountered in practice" is false for at least one entry

Current: "We group separately a class of defects whose common property is that they produce plausible incorrect values rather than detectable failures. Each was encountered in practice."

Why wrong: the depth-scale hazard (556-562) rests on line 493-494, "One test pins the consequence of the incorrect depth scale at exactly ten times" - a test, not an encounter. It cannot have been encountered: the D405 was the only depth-producing device used and its replacements yield no depth (613-615), so no code ever ran across both scale conventions on this bench. Likewise "Enumeration orders additionally differ between tools on the same host" (572-574) has no corresponding observation in Section 4.

Replace with: "We group separately a class of defects whose common property is that they produce plausible incorrect values rather than detectable failures." Then tag each of the six entries with its evidence class - *encountered on hardware*, *demonstrated in test*, or *inferred from specification* - and mark the depth-scale entry *demonstrated in test; the two scale conventions were never exercised on the same bench*.

## 16. Section 3.7, lines 410-423 - one hand-pressed switch does not establish "no endstop on any axis", and the stall mechanism is asserted with no observation

Current: "The liquid handler in our deployment has no functional endstop on any axis. This was established rather than assumed: the limit-switch state was polled for 18 seconds while a switch was depressed by hand and no bit changed... First, a stalled axis still has its steps issued, the move completes on schedule, and the position counter continues to advance... Re-observing the end-effector is the only mechanism that detects this..."

Why wrong: (a) one switch, one 18-second window, poll rate unstated, register and bit mapping never verified; the competing explanation - reading the wrong register or misparsing the reply - is not excluded and is the more common cause of "no bit changed". The all-axis conclusion in fact rests on the separate firmware-config finding, not on the poll. (b) The stall/counter-divergence mechanism appears in no Section 4 measurement, yet is stated as established fact - while the session's actual measured evidence for it (position counters silently re-zeroing twice, the gantry travelling 50 mm past a limit the reasoning agent asserted was mechanical) sits in `FINDINGS.md` finding 3 and is absent from this document. The paper simultaneously overclaims the mechanism and omits the evidence for it. (c) "the only mechanism" is false: encoder feedback, current or torque monitoring, step-loss detection, or fitting a switch all detect a stalled axis.

Replace with: "The liquid handler in our deployment has no functional endstop registering with the board. This was established rather than assumed, on two independent grounds: the Z limit-switch state was polled at [rate] Hz for 18 seconds while that switch was depressed by hand and no bit changed, and the firmware configuration contains no axis limit entries for any axis. The poll was performed on Z only; the extension to all axes rests on the configuration evidence... First, a stalled axis still has its steps issued, the move completes on schedule, and the position counter continues to advance, so the internal coordinate diverges from physical reality while every software signal reports success. We observed this directly: the position counter re-zeroed silently twice, and a move the control layer computed as beyond the mechanical limit ran and travelled a further 50 mm (Findings, item 3). Re-observing the end-effector is the only mechanism available on this machine that detects this - encoder feedback or step-loss detection would also serve, and neither is present..."

## 17. Section 3.1, lines 200-201 - "standard library only" cannot coexist with the taxonomy

Current: "This step uses only the language standard library, so it runs on a bare interpreter with no virtual environment." (Repeated in Reproducibility, line 693-694.)

Why wrong: `no_raw_usb` requires "the total device count from the USB library" (230-232), `backend_missing` presupposes third-party backends, and `exclusive_access` plus the SIGSEGV containment require librealsense. The dependency-free path cannot produce most of the taxonomy, so "runs on a bare interpreter" and "every failure resolves to a named diagnosis" cannot both hold of the same program.

Replace with: "This step uses only the language standard library, so it runs on a bare interpreter with no virtual environment. On a bare interpreter the gate can return only `no_device` or a successful enumeration; the diagnoses `no_raw_usb`, `backend_missing`, `exclusive_access`, `driver_claimed` and `opened_but_no_frames` require the optional USB and vendor libraries, whose absence is itself reported as `backend_missing` rather than inferred."

## 18. Section 3.2, lines 218-219 - two remedies are contradicted or unmeasured by the paper's own results

Current: `driver_claimed` -> "Use the OS capture path, or another host"; `exclusive_access` -> "Release the holder, or elevate", supported by line 289 "Under elevation the exclusive lock is not final."

Why wrong: the OS capture path is reported as refused to the host process (469), so the table recommends the thing the paper says was unavailable. And Section 4.1 reports only unprivileged eviction attempts - "30 unprivileged eviction attempts across three rounds failed" (458) - so no privileged result exists anywhere in the paper. The elevation remedy, and the claim that motivates the guard fix at 289-292, are unmeasured. This is the paper's own aphorism turned on its taxonomy.

Replace with: add a validation column to the taxonomy table stating, per remedy, *validated*, *unvalidated* or *unavailable on this platform*; mark the OS capture path *unavailable to the host process in our configuration (Section 4.2)* and elevation *unvalidated*. Amend line 289 to "Under elevation the exclusive lock is expected not to be final; we did not test a privileged claim, so the guard's suppression of that path is a defect on the documented workaround rather than on a measured success."

## 19. Section 3.5, lines 331-333 - the cap-height figure has no source and is probably the wrong quantity

Current: "Removing a cap uncovers a surface further from an overhead camera by approximately the cap height, 15 to 20 mm for the consumables considered."

Why wrong: no consumable is named anywhere in the paper, no catalogue number, no measurement. More seriously, a screw cap threads over the tube, so the depth delta an overhead camera sees is the rim-to-cap-top height, which is smaller than the cap's overall height. The paper uses the larger quantity and then divides 15 mm by a noise estimate in Section 4.4 to establish feasibility, so an overstated delta inflates the margin for both devices.

Replace with: "Removing a cap uncovers a surface further from an overhead camera by the height of the cap above the tube rim, which for [named consumable, catalogue number] we measured with calipers at [X] mm (cap overall height [Y] mm; the exposed delta is the smaller quantity)." Propagate the measured value through Section 4.4 and Section 4.3.

## 20. Abstract line 49 and Section 3.3 line 258 - SIGSEGV is not raised, and no version identifies the build

Current, abstract: "the Intel RealSense SDK on macOS raises SIGSEGV rather than an exception when it cannot claim a device's USB interfaces". Current, 3.3: "the bundled build **raises SIGSEGV** rather than a language-level exception".

Why wrong: (a) a segmentation fault is delivered to the process, not raised; in a paper whose second contribution is precisely the signal-versus-exception distinction, the wrong verb is an own goal. (b) The abstract names a vendor product and an OS as a class, while Section 6 (627-629) scopes the finding to one SDK build. (c) For a contribution that *is* a specific SDK fault, no SDK version, macOS version, Python version or camera firmware version is given anywhere in this document - `FINDINGS.md:260` already identifies the build as 2.56.5, so the information exists and was dropped.

Replace abstract with: "a bundled librealsense build (2.56.5) on macOS [version] terminates on SIGSEGV rather than raising an exception when it cannot claim a device's USB interfaces". Replace 3.3 with "the bundled 2.56.5 build **terminates on SIGSEGV** rather than raising a language-level exception". Add SDK, OS, Python and camera firmware versions to Reproducibility.

## 21. Section 3.6 line 373 versus lines 403-406 - "the correct model" is contradicted two paragraphs later, and the limitation never reaches Section 6

Current, 373: "For a fixed camera over a planar workspace this is the correct model rather than a degraded substitute." Current, 403-406: "a container mouth elevated above the deck projects differently from the deck beneath it, and this model cannot distinguish them."

Why wrong: the verification target in Section 3.5 is a cap 15-20 mm above the plane. The depth-free method, offered in the abstract as what "permits vision-guided positioning when metric depth is unavailable", does not cover the geometry of the task the paper is about - so contributions 4 and 5 do not compose. The limitation appears once as a caveat in 3.6 and never in Section 6, the abstract, or the conclusion.

Replace 373 with: "For a fixed camera over a planar workspace this is the correct model for targets on that plane, rather than a degraded substitute." Add to Section 6: "**The depth-free method and the closure measurement do not currently compose.** The homography is valid only for targets lying on the calibrated plane, and the closure targets sit 15 to 20 mm above it. Positioning to a deck feature is covered; measuring an elevated cap face is not."

## 22. Reproducibility, line 693, versus line 702 - "reproducible" is presently false

Current, 693: "All simulation results are reproducible with no hardware attached." Current, 700-702: "Data and code availability: To be completed prior to submission."

Why wrong: with no code released, no thresholds stated in the text (see item 30), no SDK, OS or firmware versions, and no commit identifier, nothing in the paper is reproducible by a reader. The claim is a statement about the authors' own machine.

Replace with: "All simulation results run with no hardware attached and are reproducible from the released repository at commit [SHA]; the test suite referenced in Section 4.3 is the same suite, at that commit. Until the repository is released this claim should be read as a description of our own environment."

## 23. README.md line 49 - "Everything here was observed on hardware in one session" is false for four of the five evidence blocks

Current: "Everything here was observed on hardware in one session. Where a number is measured it is stated as measured; where it is inferred it is marked inferred."

Why wrong: the supplement's four largest evidence blocks - height-delta (11 tests), homography (11), hand-eye (8 plus 5), containment (7) - are simulation-only per Section 6, and Section 4.4 is datasheet transcription. If both documents are posted together, the README's provenance claim contradicts the paper's own limitations section.

Replace with: "The findings in `FINDINGS.md` were observed on hardware in one session (2026-07-25/26). The mitigations in `SUPPLEMENT-methods-and-mitigations.md` are largely validated in simulation only; each result there states its evidence class. Where a number is measured it is stated as measured; where it is inferred it is marked inferred." Also reconcile README line 5 ("three cameras") with the supplement's account of one D405 replaced by two general-purpose devices, at most two concurrently.

## 24. Author contributions, lines 706-707 - claims work for which the paper has no result

Current: "To be confirmed. Broadly: manipulation and end-effector design; perception and verification; integration, liquid-handler control and orchestration."

Why wrong: Section 6 states "No manipulator motion was commanded. The robotic arms were unreachable on the deployment network throughout." There is no manipulation or end-effector result in the paper.

Replace with: "To be confirmed. Contributions to this paper: perception and verification; camera acquisition, containment and platform diagnosis; liquid-handler control, integration and orchestration." Also resolve line 26, "Author list and affiliations to be confirmed before submission" - a reviewer reads that as a draft that should not have circulated.

## 25. Abstract lines 35-36 and Section 1.1 line 107 - frequency claims from n=1 self-observation

Current: "We argue that the dominant failure mode of these systems is not misclassification but **silent success**"; "We take **silent success** to be the primary hazard in verified autonomy"; line 182: "the default in practice is frequently the opposite."

Why wrong: "dominant", "primary" and "frequently" are empirical frequency claims. The evidence is one stub in the authors' own codebase, and Section 2 cites nothing. No survey, no incidence data, no external system examined.

Replace with: "We argue that a systematically under-reported failure mode of these systems is not misclassification but **silent success**"; "We take **silent success** to be a hazard of a different kind from misclassification, because it has no error rate, and we organise the rest of this paper around structural measures that make it unrepresentable rather than merely unlikely. We have not surveyed deployed systems and make no claim about its frequency"; and "where in our experience the default is often the opposite."

## 26. Abstract line 44 versus Section 1.3 - the contribution count does not match

Current, abstract: "We report four contributions." Section 1.3 lists seven bullets; the Conclusion (677-681) names five.

Why wrong: three different counts in three sections. The preflight gate is contribution 1 in Section 1.3 and named first in the Conclusion, but is missing from the abstract's four; so are the height-delta method and the hazard catalogue, the latter described in a separate unnumbered abstract paragraph.

Replace with: make the enumeration and its order identical in the abstract, Section 1.3 and the Conclusion. If seven is the real number, say "We report seven contributions" and list them in the same order in all three places.

## 27. Section 4.1, lines 440-443 - changing content does not establish liveness

Current: "An MJPEG transport delivered 21 JPEG frames in four seconds over HTTP with correct multipart framing, with scene content changing between frames, confirming a live feed rather than a repeated cached frame."

Why wrong: frame-to-frame difference rules out a single cached frame and nothing else. A pre-recorded loop, a stale ring buffer, or a replayed file all pass. In a paper whose thesis is that "every software signal along the path reports normally" while nothing is observed, accepting difference as proof of liveness reproduces the paper's own failure mode one layer up.

Replace with: "...with scene content changing between frames, confirming that the transport was not serving a single cached frame. We did not establish liveness by injected physical stimulus, so a replayed or buffered source is not excluded by this observation." Better: introduce a known physical change and confirm it appears, then claim liveness.

## 28. Section 4.3, lines 490-494 - assertion tolerances are reported as measurement accuracies

Current: "The measurement recovers a 250 mm standoff to within 0.3 mm from a median over 3,600 pixels. A 15 mm step is detected with the expected margin. A 95 percent dropout rate yields `UNKNOWN` rather than a verdict, while a 30 percent dropout rate still measures to within 0.5 mm." Also 504-511: "recovered to 1 part in 10^6", "to 1 part in 10^9", "A fit survives 0.5 mm labelling noise".

Why wrong: at the paper's own 2.5 mm per-pixel sigma, the standard error of a median over 3,600 samples is about 0.05 mm, roughly seven times better than the reported 0.3 mm - so 0.3 mm is the assertion tolerance in the test, not the measured error (the code confirms `approx(STANDOFF_M, abs=0.0003)`). The 1-part-in-10^6 and 1-part-in-10^9 figures on noiseless synthetic data measure library round-trip precision, not the method. "Survives 0.5 mm noise" states no error metric and no pass criterion, so it is unfalsifiable. Section 4.3 also never says which device's noise figure seeded the simulation ("using the published close-range noise figure").

Replace with: "11 tests over synthetic depth frames at a known standoff with a known step, seeded with the short-range device's published close-range noise figure ([value], see Section 4.4). The tests assert recovery of a 250 mm standoff within a 0.3 mm tolerance from a median over 3,600 pixels, and within 0.5 mm at 30 percent dropout; these are the assertion bounds, not measured error distributions. A 95 percent dropout rate yields `UNKNOWN` rather than a verdict. One test pins the consequence of an incorrect depth scale at exactly ten times." For the homography and rigid fit: "recovered to within numerical round-off on noiseless input (a conditioning check, not an accuracy result)", and state the error metric and pass bound for each noise-injection test.

## 29. Section 6, lines 631-633 - "representative" is unjustified and the session count is missing

Current: "**Some measurements are single-trial.** The frame counts and pixel-validity figures in Section 4.1 are representative observations, not distributions over repeated trials."

Why wrong: the two acquisitions in Section 4.1 differ by about 148,000 valid pixels - a 16 percent swing - with no explanation of what changed; calling either "representative" of an unmeasured distribution is unjustified. And the entire hardware evidence base is one instrumented session (2026-07-25/26 per the README), which the paper never states.

Replace with, as the first limitation in Section 6: "**All hardware observations derive from a single instrumented session (2026-07-25/26).** The frame counts and pixel-validity figures in Section 4.1 are individual observations, not distributions over repeated trials, and the two acquisitions reported differ by roughly 16 percent in nonzero-pixel count for reasons we did not isolate. They should be read as existence proofs that the path delivered data, not as performance figures."

## 30. Section 2, lines 158-160 - the OpenCV claim overstates what the module implements

Current: "Square-marker systems, including ArUco and AprilTag families, are standard for workspace localisation and are implemented in OpenCV's `objdetect` module."

Why wrong: the module name is right (ArUco moved from contrib into `objdetect` at OpenCV 4.7), but OpenCV ships AprilTag *dictionaries* detected by the *ArUco* detector plus an AprilTag-2-derived corner-refinement mode. It does not implement Olson's AprilTag detector, which is why downstream projects ship both pipelines with different speed and accuracy characteristics.

Replace with: "Square-marker systems are standard for workspace localisation. ArUco detection, including AprilTag dictionaries such as `tag36h11`, is available in OpenCV's `objdetect` module (4.7 and later); the reference AprilTag detector (Olson 2011; Wang and Olson 2016) is a separate implementation with different performance characteristics. The `tag36h11` family is used in the system described here."

---

# SHOULD FIX (weak, unclear, or unsupported but not wrong)

## 31. No parameter table; every threshold is referred to and never stated

Locations: 349-351 ("Below a minimum valid fraction and a minimum absolute pixel count") - values are `MIN_VALID_FRACTION = 0.35` and `MIN_VALID_PIXELS = 150` in the code; 390-391 ("via the second singular value of the centred point set") - no threshold, and a singular-value test without a threshold is not a test; 506 ("A swapped correspondence is detected by residual") - no residual bound; 353-355 (confidence as a margin) - no decision cutoff; 315 (`UNKNOWN` when "the measured change matches neither hypothesis") - no tolerance. The code has `MAX_RMS_MM = 2.0` and `MIN_POINTS_FOR_TRUST = 5` and even carries the paper's rationale as a comment.

Add: a parameter table listing every threshold, its value, and its provenance (datasheet / simulation / chosen). Section 3.5's own rule - "unit conversion occurs at one reviewable site" (344) - is the same principle applied to units; apply it to thresholds. In a paper whose Section 7 argues "Refusal conditions deserve the attention normally given to accuracy", withholding all of them is self-defeating.

## 32. No false-refusal rate for a fail-closed system

Current, 300-302: "a workflow gating on `result.ok` halts at its first step rather than running to completion. We consider the halt correct and the prior green run meaningless."

Why weak: the paper never reports how often the system refuses on real data, which is the number that determines deployability. Its own text predicts the answer adversely: dropouts "concentrate on the surfaces of interest, which are frequently transparent or specular" (346-347), and 95 percent dropout yields `UNKNOWN` (492). Screw-capped tubes are commonly translucent.

Add to Section 6: "**We report no false-refusal rate.** On transparent or specular consumables the dropout fraction may routinely exceed our validity threshold, in which case the method returns `UNKNOWN` in normal operation and the policy is not deployable as specified. Establishing the real dropout distribution on the deployed consumables is the precondition for the next result."

## 33. Section 3.4, 306-307 versus 321-323 - confidence zero means two opposite things

Current: "confidence remains zero so any confidence-weighted aggregation gives it no weight" and "A channel that produces a confident negative observation, by contrast, should contribute zero, because that is evidence."

Why unclear: abstention and negative evidence - the distinction the paper calls essential (323-324) - are encoded on the same numeral. The paper then notes its own first implementation got this wrong, which is unsurprising given the representation. Separately, Section 6 says the deployed configuration is two-dimensional and fiducial-based, so it is unclear what channels are being fused in deployment at all.

Add: "The two cases must not share an encoding. We represent abstention as the absence of a contribution - the channel is excluded from the denominator - and a negative observation as a value of zero with unit weight. Collapsing both onto the number zero is what our first implementation did." And scope the fusion result: "The deployed configuration is fiducial-based and has one verification channel, so the fusion rule is validated in simulation only."

## 34. Section 4, line 429 - the declared two-way evidence split is broken three ways, and Methods contains Results

Current: "We separate results measured on hardware from those established in simulation."

Why weak: Section 4.4 is neither - it is datasheet transcription plus arithmetic. Meanwhile hardware measurements sit in Methods, outside the declared separation: line 266 ("We measured the fault directly. The full test suite exited 139..."), lines 411-413 (the 18-second limit-switch poll), lines 286-292 (the guard that suppressed its own remedy). For a paper claiming to be "explicit throughout about which results were measured on hardware" (63-64), this is self-refuting.

Fix: retitle 4.4 as "4.4 Sensor selection: datasheet analysis, not experimentally validated"; amend line 429 to "We separate three classes of result: measured on hardware, established in simulation, and derived from published specifications"; move the Methods measurements into Section 4 and cross-reference them from Methods.

## 35. Section 4.3, line 518 - 136 of 184 tests are unaccounted for

Current: "Total: **184 tests passing with no camera and no robot attached.**"

Why weak: the enumeration above it sums to 11 + 11 + 8 + 5 + 7 = 42 (the 6 fusion tests are excluded from the total), so 142 tests have no description, grouping, or claim about coverage. The one aggregate number in the paper is mostly opaque. The 184 figure is measured and correct (`184 passed` at commit `2220675`), so this is presentation only. Separately, a suite pass count is not a hardware measurement and does not belong under the heading "4.1 Measured on hardware" (line 452); what was measured on hardware is that the suite stopped crashing.

Fix: state the suite size once with its commit, and add one sentence accounting for the remainder, e.g. "The remaining 142 tests cover the API surface, teach and workflow layers, arm limits and world-model components, and are not claims of this paper." Move the 173 figure's framing under a sentence that names it as a suite-level effect rather than a hardware measurement.

## 36. Section 3.3, line 282 versus Section 5, lines 570-572 - two absolute rules conflict

Current, 3.3: "**Claim viability must not be cached, while enumeration may be.**" Current, 5: "Device indices and capture-layer unique identifiers both change when a device re-enumerates, which occurs on every reconnection. Neither may be persisted."

Why unclear: cached enumeration output contains exactly the identifiers Section 5 forbids persisting; both rules are stated absolutely. Compounding it, line 463-465 reports the location identifier changing "within one session", while Section 5's mechanism is reconnection - so either a reconnection occurred and should be stated, or Section 5's causal account is incomplete.

Fix: give the enumeration cache a lifetime and an invalidation trigger ("the enumeration cache is process-local and invalidated on any USB topology change; identifiers are never written to disk or to configuration"), and state whether the within-session identifier change followed a reconnection.

## 37. Section 2 has no citations, and Section 7's aphorisms carry n=1

Current, 155-156: "We situate this work informally rather than attempting a comprehensive survey"; line 711: "Formal citations to be completed prior to submission."

Why weak: there is no engagement with prior work on robotic success detection, action verification, or anomaly detection - the literature this paper's central problem sits in. The fail-safe claim ("long established in safety engineering", 179-180) has no citation. "Informally" is a stylistic choice; zero related work is a rejection reason. Separately, Section 7's three "structural observations" are one anecdote, one unmeasured assertion about human attention ("A crashed test suite... is easier to miss" - self-refuting, since the authors noticed it), and one normative preference, and 645 draws "The general lesson" from a single fault while Section 6 disclaims exactly that generalisation.

Fix: cite the success-detection and action-verification literature and distinguish this contribution from it; cite the fail-safe principle properly; add Hartley, "In Defense of the Eight-Point Algorithm" (1997) for the normalisation at 170-171; retitle Section 7 to "Three design positions" and attach n to each; delete "and is easier to miss" or support it with CI evidence.

## 38. Datasheet citation, line 176 and line 713 - the document number does not pin an edition

Current: "the product family datasheet (document 337029-017)".

Why weak: the September 2023, March 2024, October 2024 and 2025 editions all carry `337029-017` on the cover while incrementing a separate revision field. The min-Z and noise values quoted are correct in the September 2023 edition (Tables 4-11 and 4-14); whether the newest edition still carries them is unverified.

Replace with: "the product family datasheet (document 337029-017, rev. 017, September 2023; Tables 4-11 and 4-14)."

## 39. Section 4.4 treats min-Z as immutable

Current, 534-535: "At a 250 mm standoff, the D435 produces no depth at 1280x720 and must operate at reduced resolution near its minimum range."

Why weak: Intel documents lowering min-Z via the Advanced Mode `disparityShift` parameter, with recommended values reaching roughly 90 mm on D435-class devices, traded against maximum range - a trade that costs nothing at a fixed 250 mm bench standoff. The claim holds only at default tuning. Given that Section 4.4 already concedes the comparison was never validated, the honest move is the caveat, not a defence of the margin.

Add: "This holds at default tuning. The disparity shift can be raised to lower the minimum range on the general-purpose devices, at the cost of maximum range, which is an acceptable trade at a fixed standoff; we did not evaluate depth quality under that setting."

## 40. Abstract line 52 - "four agents" is never substantiated in the body

Current: "motivated by an instance in which four agents returned `ok=True` with `confidence=0.0` from unimplemented stubs".

Why weak: the number is correct - the codebase at `aed62fe` has `CapRemovedAgent`, `GraspSecureAgent`, `TubeAlignedAgent` and `AspirationAgent` all returning `ok=True, confidence=0.0, detail="stub"`, corroborated in `docs/OPEN_QUESTIONS.md:35` - but Section 1.1 shows one function and refers to "a verification agent", so the number appears nowhere in the body.

Fix: name them in Section 1.1: "Four agents in the dispatch table - cap-removed, grasp-secure, tube-aligned and aspiration - were implemented this way."

## 41. Tone: the paper announces its own honesty, which converts every gap above into a broken promise

Current, abstract 63-66: "We are explicit throughout about which results were measured on hardware and which were established only in simulation. The central claim of this work concerns how a system should behave when it cannot see, and it would be self-defeating to overstate what we observed." Current, 598-599: "We state these plainly, since a paper about not overclaiming is an unusually poor place to overclaim." Current, 683-687: "The system we describe verifies less than we initially set out to build... We report it in that state deliberately. The alternative was a system that appeared to verify more, which is the failure this work is about." Current, 660-661: "The accuracy of the common case is comparatively easy."

Why weak: items 1-6, 14 and 15 above falsify the abstract's claim of explicitness. Opening Limitations by praising the author's candour hands the reviewer a weapon: every defect becomes "the authors told us this was the wrong place to do this". The conclusion reframes an unvalidated system as a contribution - reporting honestly is a precondition for publication, not a result. And the paper reports no accuracy result on real data for anything, so declaring accuracy "comparatively easy" dismisses the work it did not do.

Replace: delete the abstract paragraph at 63-66 and instead label each result with its evidence class where it appears. Replace 598-599 with "The following are the limitations we are aware of." Replace 683-687 with "The closure measurement awaits a physical trial, the metric depth path is unavailable in the deployed configuration, and no calibration has been performed on hardware. We report the system in that state." Delete the final sentence of 660-661. Also soften line 266 ("We measured the fault directly") to "We reproduced the fault", since a crashed suite is a symptom rather than a direct measurement, and line 323-324 ("essential in any fused verification scheme") to "essential in the fused scheme described here", as it generalises from one implementation bug in the authors' own code.

## 42. One review-brief item is not an author error - do not "fix" it

The prompt circulating with these reviews refers to "13 rigid hand-eye tests". The paper claims 8 hand-eye tests plus a further 5 for marker detection (509-512), and the code confirms `test_ot_hand_eye.py` = 8 and `test_fiducials.py` = 5, both exact. The paper is correct here. Note also that the `test_fusion.py` in the 184-test total is a different file (5 world-model point-fusion tests) from the 6 cap-depth fusion tests described at 496-502 (`backend/tests/test_cap_depth_channel.py`, on `feat-cap-depth-channel`); consider naming the file in Section 4.3 so a reader cannot conflate them.