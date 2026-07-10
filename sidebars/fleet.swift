// ⚓ cmux-fleet sidebar — the live fleet as collapsible conductor→worker groups.
//
// NATIVE-FIRST. Each agent owns a workspace, so almost everything comes from cmux's own fields:
//     label  w.title      ctx bar  w.progress      last message  w.latestMessage
//     tap    workspace.select(w.id)
// Only STATE, PARENT and the COLLAPSE bit are pushed by `fleet paint --sidebar`, riding in a short
// workspace description that reads as prose in the built-in sidebar instead of clobbering it:
//     child      "working · ↳berg-sandbox"     (↳ = my parent conductor)
//     conductor  "ready · ▾berg-sandbox"       (▾ expanded / ▸ collapsed; carries its OWN label,
//                                               because a conductor's TITLE is decorated)
//     shared ws  "… · +2"                      (agents still sharing one workspace)
//
// COLLAPSE without @State: the chevron rewrites this workspace's description with the glyph flipped;
// `fleet paint` carries the glyph forward, so a repaint never clobbers the choice.
//
// STRUCTURE follows Examples/CustomSidebars/status-board.swift: no top-level `let`, no array-returning
// funcs. Arrays are bound with `let` INSIDE the view body and passed into view helpers.

// Reach EVERY optional with `if let`. The interpreter evaluates `!= nil` / `== nil` to nothing, so a
// guard like `if w.description != nil { ... }` is never true and the field silently reads as absent.
func descOf(_ w) -> String {
  if let d = w.description { return d }
  return ""
}
func isOurs(_ w) -> Bool {
  let d = descOf(w)
  return d.contains(" · ↳") || d.contains(" · ▾") || d.contains(" · ▸")
}
func isConductor(_ w) -> Bool {
  let d = descOf(w)
  return d.contains(" · ▾") || d.contains(" · ▸")
}
func isCollapsed(_ w) -> Bool { return descOf(w).contains(" · ▸") }

func stateOf(_ w) -> String {
  let p = descOf(w).split(separator: " ")
  if p.count == 0 { return "idle" }
  return String(p[0])
}
// first token after a glyph — the label (conductor) or parent (child) it introduces
func labelOf(_ w) -> String {
  let d = descOf(w)
  if d.contains("▾") {
    let a = d.split(separator: "▾")
    if a.count < 2 { return "" }
    return String(a[1].split(separator: " ")[0])
  }
  let b = d.split(separator: "▸")
  if b.count < 2 { return "" }
  return String(b[1].split(separator: " ")[0])
}
func parentOf(_ w) -> String {
  let d = descOf(w)
  let a = d.split(separator: "↳")
  if a.count < 2 { return "" }
  return String(a[1].split(separator: " ")[0])
}
func isChildOf(_ w, _ key) -> Bool {
  return isOurs(w) && !isConductor(w) && parentOf(w) == key
}

// flip the collapse glyph in place — the whole toggle, no @State needed
func toggled(_ w) -> String {
  let d = descOf(w)
  if d.contains("▾") {
    let a = d.split(separator: "▾")
    return "\(a[0])▸\(a[1])"
  }
  let b = d.split(separator: "▸")
  return "\(b[0])▾\(b[1])"
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

// last two path segments of a cwd (repo/leaf), so the row shows where the agent actually works
func tailPath(_ p) -> String {
  let parts = p.split(separator: "/")
  if parts.count == 0 { return p }
  if parts.count == 1 { return String(parts[0]) }
  return "\(parts[parts.count - 2])/\(parts[parts.count - 1])"
}

// ctx bar, hand-rolled: `ProgressView` renders its own VALUE as a label ("0.41000000003"), so it can't be
// used here. The shapes have no intrinsic size and inflate — every one needs an explicit .frame clamp.
// Bar and percent share ONE line.
func ctxRow(_ w) -> some View {
  if let p = w.progress {
    let remain = (1.0 - p.value) * 100.0
    return AnyView(HStack(spacing: 7) {
      HStack(spacing: 0) {
        RoundedRectangle(cornerRadius: 2).foregroundColor(ctxColor(remain))
          .frame(width: 78 * (1.0 - p.value), height: 5)
        Spacer()
      }
      .frame(width: 78, height: 5)
      .background { RoundedRectangle(cornerRadius: 2).foregroundColor("#2A2E37") }
      Text("\(Int(remain))%").font(.system(size: 10, design: .monospaced)).foregroundColor(.secondary)
      Spacer()
    }.frame(height: 12))
  }
  return AnyView(EmptyView())
}
func cwdLine(_ w) -> some View {                              // `directory` is always present
  return HStack(spacing: 4) {
    Image(systemName: "folder").font(.system(size: 8)).foregroundColor("#5A5A63")
    Text(tailPath(w.directory)).font(.system(size: 9, design: .monospaced))
      .foregroundColor("#6F6E77").lineLimit(1).truncationMode(.middle)
    Spacer()
  }
}
func lastLine(_ w) -> some View {
  if let m = w.latestMessage {
    return AnyView(Text(m).font(.system(size: 11)).foregroundColor(.tertiary)
      .lineLimit(2).truncationMode(.tail))
  }
  return AnyView(EmptyView())
}
// POSITIVE condition first, fall through to EmptyView. A bare `if cond { return EmptyView() }` early-exit
// is NOT honored in a `some View` func — that is why this badge painted a giant "0" box on every row.
// The .frame clamp is mandatory: the background shape has no intrinsic size and inflates without it.
func unreadDot(_ w) -> some View {
  if w.unread > 0 {
    return AnyView(Text("\(w.unread)").font(.system(size: 9, design: .monospaced))
      .foregroundColor("#0A0C10").frame(width: 14, height: 14)
      .background { Circle().foregroundColor("#F5A623") })
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
          Text(w.title)
            .font(.system(size: isCon ? 13 : 12))
            .fontWeight(isCon ? .bold : .semibold)
            .foregroundColor(isCon ? colorFor(stateOf(w)) : "#E8E8EC")
            .lineLimit(1).truncationMode(.tail)
          Spacer()
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

// the chevron is its own button: flips the glyph in this workspace's description
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
    groupView(c, mine.filter { isChildOf($0, labelOf(c)) }.sorted { $0.title < $1.title })
  }

  Spacer()
}.padding(8)
