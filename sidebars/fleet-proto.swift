// ⚓ fleet-proto v2 — the fleet conductor→worker hierarchy from NATIVE data. NO description blob.
//
// v2 improvements over v1:
//   1. STALE-ANCHOR ROBUSTNESS + a deliberate "Workspaces" bucket. A "Conductor - X" anchor is crowned as a
//      real group ONLY when its conductor member X is present in the anchor's run AND that member is pinned
//      (the live discriminator that separates real fleet conductors from leftover scaffold anchors like
//      "Conductor - loom-domain-expert", which is actually an AD *child*). Everything not claimed by a
//      crowned group — orphans, Berg's Dock, ungrouped tabs — drops into an intentional "Workspaces" section
//      (neutral, never a crowned pseudo-conductor). The durable discriminator is the fleet `kind` field; once
//      it (or native groupId) is on a bindable channel, swap `pinned` for it — see the report.
//   2. State coloring — rows tint by agent STATE parsed off `progress.label` (set-status pills aren't
//      projected; state rides the label as a leading word: "working · model · effort · N% left").
//   3. Subscriptions footer — read off ONE non-agent CARRIER tab whose label is "USAGE⧗line⧗line" (painted
//      by the daemon onto a scaffold anchor), instead of stapled onto every conductor. Nothing real garbles.
//
// Reads ONLY native bindable fields (never `w.description`): title, index-order, pinned, progress.value/
// .label, latestMessage, directory, unread, selected. Rows tap workspace.select.
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

func dirTail(_ w) -> String {
  let segs = w.directory.split(separator: "/")
  if segs.count == 0 { return "" }
  return String(segs[segs.count - 1])
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
// crown iff the conductor member is present AND pinned (the live real-vs-scaffold discriminator)
func isCrowned(_ p, _ ordered, _ apos) -> Bool {
  let ci = condIndexIn(p, ordered, apos)
  if ci < 0 { return false }
  return ordered[ci].pinned
}
// the crowned anchor claiming workspace i (its nearest preceding anchor, if crowned); -1 => ungrouped
func claimedBy(_ i, _ apos, _ crownedPos) -> Int {
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
func statePill(_ w) -> some View {
  let s = stateOf(w)
  if s != "" {
    return AnyView(HStack(spacing: 3) {
      Image(systemName: stateIcon(s)).font(.system(size: 8)).foregroundColor(stateColor(s))
      Text(s).font(.system(size: 9, design: .monospaced)).foregroundColor(stateColor(s))
    })
  }
  return AnyView(EmptyView())
}
func progBar(_ w) -> some View {
  if let p = w.progress {
    let f = p.value
    return AnyView(HStack(spacing: 7) {
      HStack(spacing: 0) {
        RoundedRectangle(cornerRadius: 2).foregroundColor("#3DB9A0").frame(width: 60 * f, height: 5)
        Spacer()
      }.frame(width: 60, height: 5).background { RoundedRectangle(cornerRadius: 2).foregroundColor("#2A2E37") }
      progLabelIn(p)
      Spacer()
    }.frame(height: 12))
  }
  return AnyView(EmptyView())
}
func progLabelIn(_ p) -> some View {
  if let l = p.label {
    return AnyView(Text(l).font(.system(size: 10, design: .monospaced)).foregroundColor("#8B8D98").lineLimit(1))
  }
  return AnyView(EmptyView())
}
func msgLine(_ w) -> some View {
  if let m = w.latestMessage {
    return AnyView(Text(m).font(.system(size: 11)).foregroundColor(.tertiary).lineLimit(2).truncationMode(.tail))
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
        progBar(w)
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
  let bucket = ordered.indices.filter { !isAnchor(ordered[$0]) && claimedBy($0, apos, crownedPos) < 0 }.map { ordered[$0] }
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
      ordered.indices.filter { $0 > p && $0 < nextAnchorAfter(p, apos, ordered.count) && $0 != condIndexIn(p, ordered, apos) }.map { ordered[$0] }
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
