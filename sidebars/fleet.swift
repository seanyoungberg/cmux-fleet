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
  return String(parts[1])
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
  let t = s.split(separator: "~")
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
  if s == "ready" { return "circle.dashed" }
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
// tool (field 6) — a small SF Symbol, no box. Only mark non-claude; claude is the default and stays bare.
func toolIcon(_ w) -> some View {
  if toolOf(w) == "codex" {
    return AnyView(Image(systemName: "chevron.left.forwardslash.chevron.right")
      .font(.system(size: 10)).foregroundColor("#D0A46C"))
  }
  return AnyView(EmptyView())
}

// ctx bar (field 3), hand-rolled: `ProgressView` renders its own VALUE as a label, and a bare shape has no
// intrinsic size, so both containers need an explicit .frame clamp. Bar + percent + meta share ONE line.
func ctxRow(_ w) -> some View {
  if ctxOf(w) == "-" { return AnyView(EmptyView()) }        // no ctx (e.g. codex/pending) -> no bar
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
func cwdLine(_ w) -> some View {                             // cwd (field 9) — already the repo/…/leaf tail
  let p = cwdOf(w)
  if p == "-" || p == "" { return AnyView(EmptyView()) }
  return AnyView(HStack(spacing: 4) {
    Image(systemName: "folder").font(.system(size: 8)).foregroundColor("#5A5A63")
    Text(p).font(.system(size: 9, design: .monospaced))
      .foregroundColor("#6F6E77").lineLimit(1).truncationMode(.middle)
    Spacer()
  })
}
func lastLine(_ w) -> some View {                            // last message (field 11) — from the snapshot
  let m = lastOf(w)
  if m == "-" || m == "" { return AnyView(EmptyView()) }
  return AnyView(Text(m).font(.system(size: 11)).foregroundColor(.tertiary)
    .lineLimit(2).truncationMode(.tail))
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
          toolIcon(w)
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
}.padding(8)
