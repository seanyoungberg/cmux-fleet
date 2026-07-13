// ⚓ cmux-fleet sidebar — the live fleet as collapsible conductor→worker groups.
//
// CLI-DERIVED. `fleet paint --sidebar` writes ONE FLEET4 record per workspace into its DESCRIPTION,
// straight from the same snapshot `fleet vitals` reads — so model, effort, tool, state, ctx and the last
// message all match the CLI. (Native cmux fields drop model/effort/tool and don't match vitals for
// ctx/last-message, which is why an earlier native-first rewrite lost them.) Record, 12 fields:
//     surface~label~state~ctx~parent~kind~tool~model~effort~cwd~col~last     ('-' = empty; never blank)
// The layout (groups, collapse, anchor) is ours; every value is pushed. Rows tap to focus; hot-reloads on save.
//
// COLLAPSE without @State: field 10 (`col`) is a conductor's collapse bit. The chevron rewrites this
// workspace's description with the bit flipped; `fleet paint` reads it back and carries it forward, so a
// repaint never clobbers the choice.
//
// USAGE FOOTER: fleet-global subscription usage has no per-workspace channel, so it rides every conductor's
// description after a `⧗` (one line per subscription; append-only superset shape documented at usageField
// below, e.g. `…⧗email~0~5h~15~7d~94~56m~claude~berg-max~2d~Fable~95~2d⧗…`). The footer reads it off the
// first conductor. `⧗` is stripped from record text, so the record parse above is never affected.
//
// INTERPRETER RULES (each fails SILENTLY — a wrong guard just renders nothing, no error anywhere):
//   • reach optionals with `if let`, never `== nil` / `!= nil` (those evaluate to nothing);
//   • a helper returns a String or a View, never an array — bind arrays with `let` in the view body;
//   • bind a `.split(...)` to a `let` before indexing it; clamp every bare shape with an explicit `.frame`.

func descOf(_ w) -> String {
  if let d = w.description { return d }
  return ""
}
func isOurs(_ w) -> Bool { return descOf(w).hasPrefix("FLEET4;") }

// the workspace's FIRST record — conductor-first ordering means a group's lead is record 0
func recStr(_ w) -> String {
  let d = descOf(w)
  let parts = d.split(separator: ";")
  if parts.count < 2 { return "" }
  let r = String(parts[1])
  let segs = r.split(separator: "⧗")            // drop the fleet-global usage tail if this ws carries it
  return String(segs[0])
}
// field i of that record; "" if absent. Every emitted field is non-empty ('-'), so split never drops one.
func fld(_ w, _ i) -> String {
  let s = recStr(w)
  let t = s.split(separator: "~")
  if t.count <= i { return "" }
  return String(t[i])
}
func labelOf(_ w) -> String { return fld(w, 1) }
func stateOf(_ w) -> String { return fld(w, 2) }
func ctxOf(_ w) -> String { return fld(w, 3) }
func parentOf(_ w) -> String { return fld(w, 4) }
func kindOf(_ w) -> String { return fld(w, 5) }
func toolOf(_ w) -> String { return fld(w, 6) }
func modelOf(_ w) -> String { return fld(w, 7) }
func effortOf(_ w) -> String { return fld(w, 8) }
func cwdOf(_ w) -> String { return fld(w, 9) }
func lastOf(_ w) -> String { return fld(w, 11) }

func isConductor(_ w) -> Bool { return kindOf(w) == "conductor" }
func isCollapsed(_ w) -> Bool { return fld(w, 10) == "1" }
func isChildOf(_ w, _ key) -> Bool {
  return isOurs(w) && kindOf(w) != "conductor" && parentOf(w) == key
}
// agents beyond the lead sharing this workspace (transitional tabs) — count the extra records
func extraCount(_ w) -> Int {
  let d = descOf(w)
  let recs = d.split(separator: ";")
  if recs.count < 3 { return 0 }
  return recs.count - 2
}

// flip the conductor's collapse bit in THIS workspace's record — the whole toggle, no @State. Only the
// safe standalone-conductor case (one record) is rewritten; a shared workspace is left untouched.
func toggled(_ w) -> String {
  let d = descOf(w)
  let recs = d.split(separator: ";")
  if recs.count != 2 { return d }
  let s = String(recs[1])
  let segs = s.split(separator: "⧗")            // drop any usage tail; paint re-appends it next cycle
  let base = String(segs[0])
  let t = base.split(separator: "~")
  if t.count < 12 { return d }
  let nc = t[10] == "1" ? "0" : "1"
  return "FLEET4;\(t[0])~\(t[1])~\(t[2])~\(t[3])~\(t[4])~\(t[5])~\(t[6])~\(t[7])~\(t[8])~\(t[9])~\(nc)~\(t[11])"
}

func colorFor(_ s) -> String {
  if s == "error" { return "#E5484D" }
  if s == "needs-input" { return "#F5A623" }
  if s == "review" { return "#3E63DD" }
  if s == "working" { return "#30A46C" }
  if s == "done" { return "#46A758" }
  if s == "ready" { return "#3DB9A0" }
  if s == "detached" { return "#A45CDB" }
  if s == "idle" { return "#8B8D98" }
  return "#6F6E77"
}
func iconFor(_ s) -> String {
  if s == "error" { return "exclamationmark.triangle.fill" }
  if s == "needs-input" { return "hand.raised.fill" }
  if s == "review" { return "eye.fill" }
  if s == "working" { return "gearshape.fill" }
  if s == "done" { return "checkmark.circle.fill" }
  if s == "ready" { return "circle.fill" }                    // teal presence dot — finished & available
  if s == "detached" { return "antenna.radiowaves.left.and.right.slash" }
  if s == "idle" { return "moon.zzz.fill" }
  return "questionmark.circle"
}
func ctxColor(_ remain) -> String {
  if remain > 50 { return "#30A46C" }
  if remain > 30 { return "#F5A623" }
  return "#E5484D"
}

// model · effort (fields 7,8) — the CLI-derived meta the native-first rewrite dropped. '-' reads as empty.
func metaText(_ w) -> String {
  let m = modelOf(w)
  let e = effortOf(w)
  let mm = (m == "-" || m == "") ? "" : m
  let ee = (e == "-" || e == "") ? "" : e
  if mm == "" { return ee }
  if ee == "" { return mm }
  return "\(mm) · \(ee)"
}
// tool — EVERY agent row (field 6) and every usage line (field 7) declares which tool it runs. Not "mark the
// exception, leave claude bare": a bare row silently means "claude", which stops being readable the moment a
// third tool exists AND leaves the split-by-provider usage footer with no way to tell claude from codex.
//
// ADDING A TOOL IS ONE LINE HERE. The spec is "<sf-symbol>~<hex>" and `toolGlyph` below is tool-agnostic.
// Verify a new symbol NAME resolves before trusting it: a bogus systemName draws NOTHING, silently — the
// same failure family as the interpreter bugs this file's rules are about (cmux#7943). Check it with:
//   swift -e 'import AppKit; print(NSImage(systemSymbolName: "your.symbol", accessibilityDescription: nil) != nil)'
func toolSpec(_ t) -> String {
  if t == "claude" { return "asterisk~#C96442" }
  if t == "codex"  { return "chevron.left.forwardslash.chevron.right~#D0A46C" }
  if t == "pi"     { return "circle.hexagongrid.fill~#6C8FD0" }
  if t == "-"      { return "" }                  // no tool for this row: draw nothing
  if t == ""       { return "" }
  return "questionmark.circle~#6F6E77"            // UNKNOWN tool: SHOW it. Never silently omit a row's tool.
}
// the chip for a tool STRING — the ONE builder, shared by agent rows and the usage footer. POSITIVE guard,
// one branch, falls through to EmptyView; bind the split to a `let` before indexing it (interpreter rules).
func toolGlyph(_ t) -> some View {
  let spec = toolSpec(t)
  if spec != "" {
    let p = spec.split(separator: "~")
    return AnyView(Image(systemName: String(p[0]))
      .font(.system(size: 10)).foregroundColor(String(p[1])))
  }
  return AnyView(EmptyView())
}

// INTERPRETER RULE (the whole point of this file's shape): a `some View` func is a view BUILDER — it
// COLLECTS every view expression whose conditional is satisfied and IGNORES `return`. So the guard must be
// POSITIVE: put the real view inside `if <have data>`, and let the function fall through to a single
// `EmptyView`. The inverse (`if <missing> { return EmptyView }` then a real fall-through) COLLECTS BOTH and
// renders the real view over an empty slot — that is the "stale usage line drew twice" bug.
//
// ctx bar (field 3), hand-rolled: `ProgressView` renders its own VALUE as a label, and a bare shape has no
// intrinsic size, so both containers need an explicit .frame clamp. Bar + percent + meta share ONE line.
func ctxRow(_ w) -> some View {
  if ctxOf(w) != "-" {                                      // have a ctx reading -> draw the bar
    let remain = Double(ctxOf(w))
    let frac = remain / 100.0
    return AnyView(HStack(spacing: 7) {
      HStack(spacing: 0) {
        RoundedRectangle(cornerRadius: 2).foregroundColor(ctxColor(remain))
          .frame(width: 78 * frac, height: 5)
        Spacer()
      }
      .frame(width: 78, height: 5)
      .background { RoundedRectangle(cornerRadius: 2).foregroundColor("#2A2E37") }
      Text("\(Int(remain))%").font(.system(size: 10, design: .monospaced)).foregroundColor(.secondary)
      Spacer()
      Text(metaText(w)).font(.system(size: 10, design: .monospaced)).foregroundColor("#7A7A85").lineLimit(1)
    }.frame(height: 12))
  }
  return AnyView(EmptyView())                               // codex / pending -> no bar
}
func cwdLine(_ w) -> some View {                             // cwd (field 9) — already the repo/…/leaf tail
  let p = cwdOf(w)
  if p != "-" && p != "" {
    return AnyView(HStack(spacing: 4) {
      Image(systemName: "folder").font(.system(size: 8)).foregroundColor("#5A5A63")
      Text(p).font(.system(size: 9, design: .monospaced))
        .foregroundColor("#6F6E77").lineLimit(1).truncationMode(.middle)
      Spacer()
    })
  }
  return AnyView(EmptyView())
}
func lastLine(_ w) -> some View {                            // last message (field 11) — from the snapshot
  let m = lastOf(w)
  if m != "-" && m != "" {
    return AnyView(Text(m).font(.system(size: 11)).foregroundColor(.tertiary)
      .lineLimit(2).truncationMode(.tail))
  }
  return AnyView(EmptyView())
}
// POSITIVE condition first, fall through to EmptyView. The .frame clamp is mandatory: the background shape
// has no intrinsic size and inflates the row without it. `unread` is a native cmux field, not fleet data.
func unreadDot(_ w) -> some View {
  if w.unread > 0 {
    return AnyView(Text("\(w.unread)").font(.system(size: 9, design: .monospaced))
      .foregroundColor("#0A0C10").frame(width: 14, height: 14)
      .background { Circle().foregroundColor("#F5A623") })
  }
  return AnyView(EmptyView())
}
func extraBadge(_ w) -> some View {                          // "+N" when agents still share one workspace
  if extraCount(w) > 0 {
    return AnyView(Text("+\(extraCount(w))").font(.system(size: 9, design: .monospaced))
      .foregroundColor("#8B8D98"))
  }
  return AnyView(EmptyView())
}

// ── fleet-global subscription usage ────────────────────────────────────────────────────────────
// cmux gives a custom sidebar NO global channel, so `fleet paint` rides the usage panel on every
// conductor's description after a ⧗, ONE line per subscription. The line is an APPEND-ONLY superset of the
// original 7-field shape — fields 0-6 keep their old meaning, 7-12 are new:
//   0 label  1 stale  2 w1L  3 w1P  4 w2L  5 w2P  6 w1reset  7 tool  8 acct  9 w2reset  10 scL  11 scP  12 scReset
//   "…⧗seanyoungberg@gmail.com~0~5h~15~7d~94~56m~claude~berg-max~2d~Fable~95~2d⧗…"
// So it renders each subscription SPLIT BY PROVIDER (a claude/codex chip), keyed by the unambiguous EMAIL
// (display_name collides — both of Berg's are "Berg"), with EACH window's own reset and the scoped weekly
// sub-limit (Fable). Reading fewer fields is safe: an un-adopted painter emits only 0-6, and this sidebar's
// positive guards make 7-12 render nothing when absent — no garble either direction. A '1' stale flag
// renders one clean "usage stale" line instead of confident-looking garbage.
func hasUsage(_ w) -> Bool { return descOf(w).contains("⧗") }
func usageField(_ s, _ i) -> String {
  let t = s.split(separator: "~")
  if t.count <= i { return "" }
  return String(t[i])
}
func usageColor(_ used) -> String {                           // by CONSUMED share of the window
  if used > 80 { return "#E5484D" }
  if used > 60 { return "#F5A623" }
  return "#30A46C"
}
// one window as "5h 15% ↻56m" — label dim, % colored by consumption, its OWN reset trailing (dim). POSITIVE
// guard: render only when the window is present, fall through to EmptyView (an absent window adds NOTHING).
// The reset is per-window: a 5h window and a 7d window each show when THEY refresh, not one shared countdown.
func usageWinReset(_ label, _ pctS, _ reset) -> some View {
  if label != "-" && label != "" && pctS != "-" {
    let used = Double(pctS)
    return AnyView(HStack(spacing: 3) {
      Text(label).font(.system(size: 11, design: .monospaced)).foregroundColor("#8B8D98")
      Text("\(Int(used))%").font(.system(size: 12, design: .monospaced)).foregroundColor(usageColor(used))
      usageReset(reset)
    })
  }
  return AnyView(EmptyView())
}
func usageReset(_ reset) -> some View {                       // "↻56m" — trails its own window; dim
  if reset != "-" && reset != "" {
    return AnyView(HStack(spacing: 1) {
      Image(systemName: "arrow.clockwise").font(.system(size: 7)).foregroundColor("#6F6E77")
      Text(reset).font(.system(size: 10, design: .monospaced)).foregroundColor("#6F6E77")
    })
  }
  return AnyView(EmptyView())
}
func usageAccount(_ acct) -> some View {                      // dim "·berg-max" — ties the email to config/dir naming
  if acct != "-" && acct != "" {
    return AnyView(Text("·\(acct)").font(.system(size: 9, design: .monospaced)).foregroundColor("#5A5A63").lineLimit(1))
  }
  return AnyView(EmptyView())
}
// A STALE/failed provider and a FRESH one render on ONE line each. Split into two POSITIVE-guarded views so
// exactly one is collected per provider (the other is EmptyView) — NOT `if stale { staleLine } freshLine`,
// which the builder collects BOTH of, drawing "usage stale" over a phantom "-% -%" row.
func usageStale(_ s) -> some View {
  if usageField(s, 1) == "1" {
    return AnyView(HStack(spacing: 6) {
      toolGlyph(usageField(s, 7))                             // provider chip (claude/codex) even when stale
      Text(usageField(s, 0)).font(.system(size: 12, design: .monospaced)).foregroundColor("#B8B8C0").lineLimit(1)
      Text("· usage stale").font(.system(size: 12)).foregroundColor("#6F6E77")
      Spacer()
    })
  }
  return AnyView(EmptyView())
}
// Two lines per subscription: [chip · email · dim config-id], then the windows each with their own reset.
// A single-window provider (codex 7d) just draws one window; the Fable scoped sub-limit draws only when set.
func usageFresh(_ s) -> some View {
  if usageField(s, 1) != "1" {
    return AnyView(VStack(alignment: .leading, spacing: 1) {
      HStack(spacing: 6) {
        toolGlyph(usageField(s, 7))
        Text(usageField(s, 0)).font(.system(size: 12, design: .monospaced)).foregroundColor("#D8D8E0").lineLimit(1)
        usageAccount(usageField(s, 8))
        Spacer()
      }
      HStack(spacing: 10) {
        usageWinReset(usageField(s, 2), usageField(s, 3), usageField(s, 6))     // w1 (5h / 7d) + its reset
        usageWinReset(usageField(s, 4), usageField(s, 5), usageField(s, 9))     // w2 (7d) + its reset
        usageWinReset(usageField(s, 10), usageField(s, 11), usageField(s, 12))  // scoped weekly sub-limit (Fable)
        Spacer()
      }.padding(.leading, 16)
    })
  }
  return AnyView(EmptyView())
}
func usageLine(_ s) -> some View {                            // exactly one of the two shows (the other = EmptyView)
  return AnyView(VStack(alignment: .leading, spacing: 0) {
    usageStale(s)
    usageFresh(s)
  })
}

func agentRow(_ w, _ isCon) -> some View {
  return Button(action: { cmux("workspace.select", workspace_id: w.id) }) {
    HStack(alignment: .top, spacing: 7) {
      VStack(alignment: .leading, spacing: 3) {
        HStack(spacing: 6) {
          Image(systemName: iconFor(stateOf(w))).font(.system(size: isCon ? 12 : 10))
            .foregroundColor(colorFor(stateOf(w)))
          Text(labelOf(w))
            .font(.system(size: isCon ? 13 : 12))
            .fontWeight(isCon ? .bold : .semibold)
            .foregroundColor(isCon ? colorFor(stateOf(w)) : "#E8E8EC")
            .lineLimit(1).truncationMode(.tail)
          toolGlyph(toolOf(w))
          Spacer()
          extraBadge(w)
          unreadDot(w)
        }
        ctxRow(w)
        cwdLine(w)
        lastLine(w)
      }
      Spacer()
    }
    .padding(6)
    .background { RoundedRectangle(cornerRadius: 6).foregroundColor(w.selected ? "#1B2029" : (isCon ? "#14171E" : "#00000000")) }
  }
}

// the chevron is its own button: flips the collapse bit in this workspace's record
func chevron(_ w) -> some View {
  return Button(action: {
    cmux("workspace.action", workspace_id: w.id, action: "set-description", description: toggled(w))
  }) {
    Image(systemName: isCollapsed(w) ? "chevron.right" : "chevron.down")
      .font(.system(size: 10)).foregroundColor("#8B8D98").frame(width: 14, height: 14)
  }
}

// `kids` is passed in — helpers never RETURN arrays (unsupported), they only take them
func groupView(_ c, _ kids) -> some View {
  return VStack(alignment: .leading, spacing: 3) {
    HStack(alignment: .top, spacing: 2) {
      chevron(c).padding(.top, 8)
      agentRow(c, true)
    }
    if isCollapsed(c) {
      Text("\(kids.count) hidden")
        .font(.system(size: 10, design: .monospaced)).foregroundColor("#6F6E77")
        .padding(.leading, 26)
    }
    if !isCollapsed(c) {
      VStack(alignment: .leading, spacing: 3) {
        ForEach(kids) { k in
          agentRow(k, false)
        }
      }.padding(.leading, 22)
    }
  }
}

VStack(alignment: .leading, spacing: 8) {
  // arrays are bound HERE, in the view body — not returned from funcs
  let mine = workspaces.filter { isOurs($0) }
  let leads = mine.filter { isConductor($0) }.sorted { labelOf($0) < labelOf($1) }

  HStack {
    Text("⚓ Fleet").font(.system(size: 16)).bold()
    Spacer()
    Text("\(mine.count)").font(.system(size: 11, design: .monospaced)).foregroundColor(.secondary)
    Text(clock.time).font(.system(size: 11, design: .monospaced)).foregroundColor(.secondary)
  }
  Divider()

  // self-diagnosing empty state: names the failing stage instead of a bare "no data"
  if mine.count == 0 {
    Text("no fleet rows matched").font(.system(size: 11)).foregroundColor("#F5A623")
    Text("\(workspaces.count) workspaces · run: fleet paint --sidebar")
      .font(.system(size: 10, design: .monospaced)).foregroundColor("#6F6E77")
    ForEach(workspaces.prefix(3)) { w in
      Text("[\(descOf(w))]").font(.system(size: 9, design: .monospaced)).foregroundColor("#6F6E77").lineLimit(1)
    }
  }

  ForEach(leads) { c in
    groupView(c, mine.filter { isChildOf($0, labelOf(c)) }.sorted { labelOf($0) < labelOf($1) })
  }

  Spacer()

  // subscription usage footer — read off the first conductor carrying a ⧗ segment (fleet-global, per
  // subscription not per agent). Bound in the body (helpers never return arrays).
  let carriers = mine.filter { isConductor($0) && hasUsage($0) }
  if carriers.count > 0 {
    Divider()
    Text("subscriptions").font(.system(size: 9, design: .monospaced)).foregroundColor("#6F6E77")
    let segs = descOf(carriers[0]).split(separator: "⧗")
    ForEach(Array(segs.dropFirst(1))) { seg in
      usageLine(String(seg))
    }
  }
}.padding(8)
