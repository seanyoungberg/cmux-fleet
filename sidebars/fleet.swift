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

func descOf(_ w) -> String {
  if w.description != nil && w.description != "" { return w.description }
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
  if s == "idle" { return "moon.zzz.fill" }
  return "questionmark.circle"
}
func ctxColor(_ remain) -> String {
  if remain > 50 { return "#30A46C" }
  if remain > 30 { return "#F5A623" }
  return "#E5484D"
}

func hasProgress(_ w) -> Bool {
  return w.progress != nil && w.progress.value != nil
}
// ctx bar straight off the native progress field (paint writes value = fraction USED)
func ctxRow(_ w) -> some View {
  if !hasProgress(w) { return AnyView(EmptyView()) }
  let remain = (1.0 - w.progress.value) * 100.0
  return AnyView(HStack(spacing: 6) {
    ProgressView(value: 1.0 - w.progress.value, total: 1.0).tint(ctxColor(remain)).frame(width: 84)
    Text("\(Int(remain))%").font(.system(size: 10, design: .monospaced)).foregroundColor(.secondary)
    Spacer()
  })
}
func lastLine(_ w) -> some View {
  if w.latestMessage == nil { return AnyView(EmptyView()) }
  return AnyView(Text(w.latestMessage).font(.system(size: 12)).foregroundColor(.tertiary)
    .lineLimit(2).truncationMode(.tail))
}
func unreadDot(_ w) -> some View {
  if w.unread == 0 { return AnyView(EmptyView()) }
  return AnyView(Text("\(w.unread)").font(.system(size: 10, design: .monospaced))
    .foregroundColor("#0A0C10").padding(.horizontal, 5).padding(.vertical, 1)
    .background { RoundedRectangle(cornerRadius: 6).foregroundColor("#F5A623") })
}

func agentRow(_ w, _ isCon) -> some View {
  return Button(action: { cmux("workspace.select", workspace_id: w.id) }) {
    HStack(alignment: .top, spacing: 7) {
      VStack(alignment: .leading, spacing: 2) {
        HStack(spacing: 6) {
          Image(systemName: iconFor(stateOf(w))).font(.system(size: isCon ? 13 : 11))
            .foregroundColor(colorFor(stateOf(w)))
          Text(w.title)
            .font(.system(size: isCon ? 14 : 13))
            .fontWeight(isCon ? .bold : .semibold)
            .foregroundColor(isCon ? colorFor(stateOf(w)) : "#E8E8EC")
            .lineLimit(1).truncationMode(.tail)
          unreadDot(w)
        }
        ctxRow(w)
        lastLine(w)
      }
      Spacer()
    }
    .padding(5)
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

  // self-diagnosing empty state: says WHICH stage failed instead of a bare "no data"
  if mine.count == 0 {
    let described = workspaces.filter { descOf($0) != "" }
    Text("no fleet rows matched").font(.system(size: 11)).foregroundColor("#F5A623")
    Text("workspaces: \(workspaces.count)")
      .font(.system(size: 10, design: .monospaced)).foregroundColor("#6F6E77")
    Text("with description: \(described.count)")
      .font(.system(size: 10, design: .monospaced)).foregroundColor("#6F6E77")
    ForEach(described.prefix(3)) { d in
      Text(descOf(d)).font(.system(size: 9, design: .monospaced)).foregroundColor("#6F6E77").lineLimit(1)
    }
  }

  ForEach(leads) { c in
    groupView(c, mine.filter { isChildOf($0, labelOf(c)) }.sorted { $0.title < $1.title })
  }

  Spacer()
}.padding(8)
