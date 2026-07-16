// ⚓ fleet-proto — the fleet conductor→worker hierarchy from NATIVE data (the live cutover sidebar).
//
// Layout: crowned conductor groups (conductor row + indented children), then a deliberate neutral
// "Workspaces" bucket for orphans / Berg's Dock / ungrouped tabs, then the fleet-global subscriptions footer.
//
//   1. CROWN discriminator: a "Conductor - X" anchor is a real group only when its conductor member X is
//      present in the anchor's run AND is a real conductor. Real-conductor test: the fleet KIND token on
//      progress.label (authoritative) when painted; else Berg's manual `pinned` state (pre-adopt fallback).
//      This is what keeps scaffold anchors (e.g. an AD *child* "Conductor - loom-domain-expert") out.
//   2. State coloring — rows tint by agent STATE, the leading word of progress.label ("working · …").
//   3. Last message — the agent's last ASSISTANT reply, NOT cmux's native latestMessage (which is the last
//      PROMPT when iMessage mode is off, the default). Source order: progress.label ⟐last (post-adopt) →
//      FLEET4 blob field 11 (transitional, live) → native latestMessage only if it differs from the prompt.
//   4. Subscriptions footer — read off ONE non-agent CARRIER tab (label "USAGE⧗line⧗line"), not every conductor.
//
// progress.label is the one projected free-text channel, so the painter overloads it: "<human>⟐<kind>⟐<last>".
// The sidebar shows only the <human> part in the ctx caption. Mostly-native: reads title, index-order, pinned,
// progress.value/.label, latestMessage/latestPrompt, directory, unread, selected — plus blob field 11 for the
// last-message ONLY, transitionally, until the ⟐last painter is adopted. Rows tap workspace.select.
//
// Interpreter rules: positive guard then EmptyView fallthrough; AnyView on branching views; `if let` not
// `!= nil`; helpers never return arrays (bind arrays in the body / inline in ForEach); `.frame` clamp on
// bare shapes; NO multi-char `.split` (unreliable — use hasPrefix / single-char separators).

func isAnchor(_ w) -> Bool { return w.title.hasPrefix("Conductor - ") }

// ── native progress.label helpers ──────────────────────────────────────────────────────────────
func plLabel(_ w) -> String {
  if let p = w.progress { if let l = p.label { return l } }
  return ""
}
// The painter rides a machine suffix on progress.label: "<human>⟐<kind>⟐<last>". ⟐ (U+27D0) is a single
// char so the split is reliable (multi-char splits are not). Absent pre-adopt → these return "" and callers
// fall back: crown by pinned, and no message (cmux's native latestMessage is the last PROMPT, not the reply).
func plHuman(_ w) -> String {
  let l = plLabel(w)
  let p = l.split(separator: "⟐")
  if p.count >= 1 { return String(p[0]) }
  return l
}
func kindOf(_ w) -> String {
  let l = plLabel(w)
  let p = l.split(separator: "⟐")
  if p.count >= 2 { return String(p[1]) }
  return ""
}
func lastOf(_ w) -> String {
  let l = plLabel(w)
  let p = l.split(separator: "⟐")
  if p.count >= 3 { return String(p[2]) }
  return ""
}
// TRANSITIONAL fallback for the last-message until the `⟐last` painter is adopted: the FLEET4 blob (still
// painted as the fleet.swift fallback) carries the same transcript-derived assistant text in field 11.
// Read ONLY that one field — everything else stays native. Drops away once progress.label carries `last`.
func descOf(_ w) -> String { if let d = w.description { return d } ; return "" }
func blobLast(_ w) -> String {
  let d = descOf(w)
  if !d.hasPrefix("FLEET4;") { return "" }
  let recs = d.split(separator: ";")
  if recs.count < 2 { return "" }
  let rec = String(recs[1])
  let segs = rec.split(separator: "⧗")                 // drop the fleet-global usage tail if this ws carries it
  let base = segs.count >= 1 ? String(segs[0]) : rec
  let f = base.split(separator: "~")
  if f.count < 12 { return "" }
  let last = String(f[11])
  if last == "-" { return "" }
  return last
}
// leading state word (single-string hasPrefix — no fragile multi-char split)
func stateOf(_ w) -> String {
  let l = plLabel(w)
  if l.hasPrefix("working · ") { return "working" }
  if l.hasPrefix("idle · ") { return "idle" }
  if l.hasPrefix("needs-input · ") { return "needs-input" }
  if l.hasPrefix("error · ") { return "error" }
  if l.hasPrefix("review · ") { return "review" }
  if l.hasPrefix("done · ") { return "done" }
  if l.hasPrefix("ready · ") { return "ready" }
  if l.hasPrefix("detached · ") { return "detached" }
  return ""
}
func stateColor(_ s) -> String {
  if s == "error" { return "#E5484D" }
  if s == "needs-input" { return "#F5A623" }
  if s == "review" { return "#3E63DD" }
  if s == "working" { return "#30A46C" }
  if s == "done" { return "#46A758" }
  if s == "ready" { return "#3DB9A0" }
  if s == "detached" { return "#A45CDB" }
  if s == "idle" { return "#8B8D98" }
  return ""
}
func stateIcon(_ s) -> String {
  if s == "error" { return "exclamationmark.triangle.fill" }
  if s == "needs-input" { return "hand.raised.fill" }
  if s == "review" { return "eye.fill" }
  if s == "working" { return "gearshape.fill" }
  if s == "done" { return "checkmark.circle.fill" }
  if s == "ready" { return "circle.fill" }
  if s == "detached" { return "antenna.radiowaves.left.and.right.slash" }
  if s == "idle" { return "moon.zzz.fill" }
  return ""
}

// last THREE path segments (repo/…/leaf), joined by "/". split on the single "/" char is reliable.
func dirTail(_ w) -> String {
  let segs = w.directory.split(separator: "/")
  let c = segs.count
  if c == 0 { return "" }
  if c == 1 { return String(segs[0]) }
  if c == 2 { return "\(segs[c - 2])/\(segs[c - 1])" }
  return "\(segs[c - 3])/\(segs[c - 2])/\(segs[c - 1])"
}

// ── group reconstruction (index-based; helpers return scalars, never arrays) ─────────────────────
func nextAnchorAfter(_ p, _ apos, _ n) -> Int {
  let a = apos.filter { $0 > p }
  if a.count > 0 { return a[0] }
  return n
}
// index of the conductor member inside anchor p's run (member titled X where anchor is "Conductor - X"); -1 if none
func condIndexIn(_ p, _ ordered, _ apos) -> Int {
  let np = nextAnchorAfter(p, apos, ordered.count)
  let at = ordered[p].title
  let hits = ordered.indices.filter { $0 > p && $0 < np && at == "Conductor - \(ordered[$0].title)" }
  if hits.count > 0 { return hits[0] }
  return -1
}
// crown iff the conductor member is present AND is a real conductor. Discriminator: the fleet KIND token on
// progress.label (authoritative) when present; else fall back to Berg's manual `pinned` state (pre-adopt).
func isCrowned(_ p, _ ordered, _ apos) -> Bool {
  let ci = condIndexIn(p, ordered, apos)
  if ci < 0 { return false }
  let k = kindOf(ordered[ci])
  if k != "" { return k == "conductor" }        // kind painted → authoritative (kills the pinned caveat)
  return ordered[ci].pinned                       // pre-adopt fallback
}
// a PAINTED fleet agent carries a progress.label (the daemon paints one for every live agent). Unpainted
// non-fleet tabs (Berg's Dock/Files/Canvas/…, stray path-titled workspaces) have none — so they bucket
// separately even when they trail a REAL conductor at the end of the order (no group-boundary field exists;
// the durable fix is the upstream groupId projection). NB: an anchor's carrier tab also carries a label,
// but anchors are excluded from children/bucket by isAnchor, so this stays exact for non-anchor rows.
func isFleetAgent(_ w) -> Bool { return plLabel(w) != "" }
// the crowned anchor claiming workspace i (its nearest preceding anchor, if crowned); -1 => ungrouped.
// Only a painted fleet agent can be claimed — an unpainted tab trailing a conductor drops to the bucket.
func claimedBy(_ i, _ ordered, _ apos, _ crownedPos) -> Int {
  if !isFleetAgent(ordered[i]) { return -1 }
  let preceding = apos.filter { $0 < i }
  if preceding.count == 0 { return -1 }
  let nap = preceding[preceding.count - 1]
  if crownedPos.contains(nap) { return nap }
  return -1
}

// ── row rendering ────────────────────────────────────────────────────────────────────────────────
func unreadDot(_ w) -> some View {
  if w.unread > 0 {
    return AnyView(Text("\(w.unread)").font(.system(size: 9, design: .monospaced))
      .foregroundColor("#0A0C10").frame(width: 14, height: 14)
      .background { Circle().foregroundColor("#F5A623") })
  }
  return AnyView(EmptyView())
}
// the ONE state indicator — up by the agent name, colored, with an icon (bigger). The old duplicate state
// word by the ctx bar is gone; the bar row now carries only the gauge + % + model·effort.
func statePill(_ w) -> some View {
  let s = stateOf(w)
  if s != "" {
    return AnyView(HStack(spacing: 4) {
      Image(systemName: stateIcon(s)).font(.system(size: 12)).foregroundColor(stateColor(s))
      Text(s).font(.system(size: 11, design: .monospaced)).foregroundColor(stateColor(s))
    })
  }
  return AnyView(EmptyView())
}
// ctx as a FUEL GAUGE: green bar anchored RIGHT, width ∝ ctx REMAINING (full at 100%, DRAINS IN FROM THE LEFT
// as it depletes — the left goes dark first, green hugs the right), threshold-colored (green >50 / amber
// 30–50 / red <30). progress.value is the CONSUMED fraction, so remaining = 1 - value. The % sits next to the
// bar; model·effort are right-aligned (pushed by a Spacer).
func ctxColor(_ remain) -> String {
  if remain > 50 { return "#30A46C" }
  if remain > 30 { return "#F5A623" }
  return "#E5484D"
}
func progRow(_ w) -> some View {
  if let p = w.progress {
    let remain = (1.0 - p.value) * 100.0
    let frac = remain / 100.0
    let c = ctxColor(remain)
    return AnyView(HStack(spacing: 7) {
      HStack(spacing: 0) {
        Spacer()
        RoundedRectangle(cornerRadius: 2).foregroundColor(c).frame(width: 66 * frac, height: 6)
      }.frame(width: 66, height: 6).background { RoundedRectangle(cornerRadius: 2).foregroundColor("#2A2E37") }
      Text("\(Int(remain))%").font(.system(size: 10, design: .monospaced)).foregroundColor(c)
      Spacer()
      Text(modelEffort(w)).font(.system(size: 10, design: .monospaced)).foregroundColor("#7A7A85").lineLimit(1)
    }.frame(height: 14))
  }
  return AnyView(EmptyView())
}
// model·effort, parsed off the HUMAN part "state · model · effort · N% left" (split on the single "·" char —
// reliable, unlike a multi-char " · " split). Common live case (state present, per-agent workspace) → parts
// [1]·[2]. A dedicated ⟐model⟐effort painter field would make this bulletproof (deferred; needs an adopt).
func modelEffort(_ w) -> String {
  let h = plHuman(w)
  let parts = h.split(separator: "·")
  if parts.count >= 4 { return "\(parts[1])·\(parts[2])" }
  if parts.count == 3 { return "\(parts[1])" }
  return ""
}
// native latestMessage is the last PROMPT (iMessage mode off), so it MUST NOT be shown as the agent's reply.
// Show the fleet's transcript-derived last ASSISTANT message off progress.label (`lastOf`); only fall back
// to native latestMessage when it demonstrably differs from the last prompt (iMessage-on case). Else nothing.
func msgToShow(_ w) -> String {
  let lm = lastOf(w)                 // 1. progress.label ⟐last (post-adopt, clean)
  if lm != "" { return lm }
  let bl = blobLast(w)               // 2. FLEET4 blob field 11 (live now, transitional)
  if bl != "" { return bl }
  if let m = w.latestMessage {       // 3. native, only if it differs from the prompt (iMessage-on edge)
    if let pm = w.latestPrompt { if m != pm { return m } ; return "" }
    return m
  }
  return ""
}
func msgLine(_ w) -> some View {
  let s = msgToShow(w)
  if s != "" {
    return AnyView(Text(s).font(.system(size: 11)).foregroundColor(.tertiary).lineLimit(2).truncationMode(.tail))
  }
  return AnyView(EmptyView())
}
func dirLine(_ w) -> some View {
  let d = dirTail(w)
  if d != "" {
    return AnyView(HStack(spacing: 4) {
      Image(systemName: "folder").font(.system(size: 8)).foregroundColor("#5A5A63")
      Text(d).font(.system(size: 9, design: .monospaced)).foregroundColor("#6F6E77").lineLimit(1).truncationMode(.middle)
      Spacer()
    })
  }
  return AnyView(EmptyView())
}

// role: "cond" (conductor) | "child" | "plain" (bucket). Accent = state color when known, else role/selected.
func accentOf(_ w, _ role) -> String {
  let sc = stateColor(stateOf(w))
  if sc != "" { return sc }
  if w.selected { return "#3E63DD" }
  if role == "cond" { return "#3E63DD" }
  return "#3A3D46"
}
func roleIcon(_ role) -> String {
  if role == "cond" { return "person.fill" }
  if role == "child" { return "arrow.turn.down.right" }
  return "circle"
}
func agentRow(_ w, _ role) -> some View {
  let isCon = role == "cond"
  return Button(action: { cmux("workspace.select", workspace_id: w.id) }) {
    HStack(alignment: .top, spacing: 7) {
      Capsule().frame(width: 3, height: 24).foregroundColor(accentOf(w, role))
      VStack(alignment: .leading, spacing: 3) {
        HStack(spacing: 6) {
          Image(systemName: roleIcon(role))
            .font(.system(size: isCon ? 12 : 9)).foregroundColor(isCon ? accentOf(w, role) : "#6F6E77")
          Text(w.title).font(.system(size: isCon ? 13 : 12)).fontWeight(isCon ? .bold : .regular)
            .foregroundColor(w.selected ? "#FFFFFF" : "#D8D8E0").lineLimit(1).truncationMode(.tail)
          statePill(w)
          Spacer()
          unreadDot(w)
        }
        progRow(w)
        dirLine(w)
        msgLine(w)
      }
      Spacer()
    }
    .padding(6)
    .background { RoundedRectangle(cornerRadius: 6).foregroundColor(w.selected ? "#1B2029" : (isCon ? "#14171E" : "#00000000")) }
  }
}

func groupView(_ c, _ kids) -> some View {
  return VStack(alignment: .leading, spacing: 4) {
    agentRow(c, "cond")
    VStack(alignment: .leading, spacing: 3) {
      ForEach(kids.prefix(24)) { k in agentRow(k, "child") }
    }.padding(.leading, 18)
  }
  .padding(.vertical, 3)
  .padding(.horizontal, 2)
  .background { RoundedRectangle(cornerRadius: 8).foregroundColor("#0E1014").opacity(0.55) }
}

// ── subscriptions footer (read off a non-agent CARRIER tab: label "USAGE⧗line⧗line") ──────────────
func usageField(_ s, _ i) -> String {
  let t = s.split(separator: "~")
  if t.count <= i { return "" }
  return String(t[i])
}
func usageColor(_ used) -> String {
  if used > 80 { return "#E5484D" }
  if used > 60 { return "#F5A623" }
  return "#30A46C"
}
func usageWin(_ label, _ pctS) -> some View {
  if label != "-" && label != "" && pctS != "-" && pctS != "" {
    let used = Double(pctS)
    return AnyView(HStack(spacing: 3) {
      Text(label).font(.system(size: 11, design: .monospaced)).foregroundColor("#8B8D98")
      Text("\(Int(used))%").font(.system(size: 12, design: .monospaced)).foregroundColor(usageColor(used))
    })
  }
  return AnyView(EmptyView())
}
func usageLine(_ s) -> some View {
  if usageField(s, 1) == "1" {
    return AnyView(HStack(spacing: 6) {
      Text(usageField(s, 0)).font(.system(size: 11, design: .monospaced)).foregroundColor("#B8B8C0").lineLimit(1)
      Text("· usage stale").font(.system(size: 11)).foregroundColor("#6F6E77")
      Spacer()
    })
  }
  return AnyView(HStack(spacing: 8) {
    Text(usageField(s, 0)).font(.system(size: 11, design: .monospaced)).foregroundColor("#D8D8E0").lineLimit(1)
    usageWin(usageField(s, 2), usageField(s, 3))
    usageWin(usageField(s, 4), usageField(s, 5))
    Spacer()
  })
}

// ── body ───────────────────────────────────────────────────────────────────────────────────────
VStack(alignment: .leading, spacing: 8) {
  let ordered = workspaces                                  // native sidebar index order
  let apos = ordered.indices.filter { isAnchor(ordered[$0]) }
  let crownedPos = apos.filter { isCrowned($0, ordered, apos) }
  let bucket = ordered.indices.filter { !isAnchor(ordered[$0]) && claimedBy($0, ordered, apos, crownedPos) < 0 }.map { ordered[$0] }
  let carriers = ordered.filter { plLabel($0).hasPrefix("USAGE") }

  HStack {
    Text("⚓ Fleet · native").font(.system(size: 15)).bold()
    Spacer()
    Text("\(crownedPos.count) grp").font(.system(size: 11, design: .monospaced)).foregroundColor(.secondary)
    Text(clock.time).font(.system(size: 11, design: .monospaced)).foregroundColor(.secondary)
  }
  Text("no description blob — native fields only")
    .font(.system(size: 9, design: .monospaced)).foregroundColor("#565F89")
  Divider()

  if crownedPos.count == 0 {
    Text("no crowned conductor groups").font(.system(size: 11)).foregroundColor("#F5A623")
    Text("\(workspaces.count) workspaces present").font(.system(size: 10, design: .monospaced)).foregroundColor("#6F6E77")
  }

  ForEach(Array(crownedPos.enumerated()), id: \.offset) { gi, p in
    groupView(
      ordered[condIndexIn(p, ordered, apos)],
      ordered.indices.filter { $0 > p && $0 < nextAnchorAfter(p, apos, ordered.count) && $0 != condIndexIn(p, ordered, apos) && isFleetAgent(ordered[$0]) }.map { ordered[$0] }
    )
  }

  if bucket.count > 0 {
    VStack(alignment: .leading, spacing: 3) {
      HStack(spacing: 6) {
        Image(systemName: "square.grid.2x2").font(.system(size: 10)).foregroundColor("#6F6E77")
        Text("Workspaces").font(.system(size: 11)).fontWeight(.semibold).textCase(.uppercase).foregroundColor("#8B8D98")
        Text("\(bucket.count)").font(.system(size: 10, design: .monospaced)).foregroundColor("#565F89")
        Spacer()
      }.padding(.top, 4)
      ForEach(bucket.prefix(30)) { w in agentRow(w, "plain") }
    }
  }

  if carriers.count > 0 {
    Divider()
    Text("subscriptions").font(.system(size: 9, design: .monospaced)).foregroundColor("#6F6E77")
    let segs = plLabel(carriers[0]).split(separator: "⧗")
    ForEach(Array(segs.dropFirst(1))) { seg in
      usageLine(String(seg))
    }
  }

  Spacer()
}.padding(8)
