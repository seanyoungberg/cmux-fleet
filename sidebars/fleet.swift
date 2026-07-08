// ⚓ cmux-fleet sidebar — the live fleet as conductor→worker groups.
//
// The sidebar can only read cmux's own data, so the board rides in through a workspace DESCRIPTION:
// `fleet paint` serializes the fleet into  "FLEET3;label~state~ctx~parent~kind~surface~tool~model~effort~cwd~last;..."  and
// writes it to one marker workspace; this file finds it by prefix, parses it, and draws the board.
// Layout is ours; data is pushed. Rows tap to focus the agent. Hot-reloads on save.

func hasBlob(_ w) -> Bool {
    if w.description == nil { return false }
    return w.description.hasPrefix("FLEET3;")
}
func blobOf(_ w) -> String {
    if w.description == nil { return "" }
    return w.description
}
func fleetRaw() -> String {
    let hits = workspaces.filter { hasBlob($0) }
    if hits.count == 0 { return "" }
    return blobOf(hits[0])
}
func agentList() -> [[String]] {
    let raw = fleetRaw()
    if raw == "" { return [] }
    let recs = raw.split(separator: ";")
    return Array(recs.dropFirst(1)).map { r in r.split(separator: "~").map { f in String(f) } }
}
func conductors() -> [[String]] {
    return agentList().filter { $0.count >= 5 && $0[4] == "conductor" }.sorted { $0[0] < $1[0] }
}
func childrenOf(_ parent) -> [[String]] {
    return agentList().filter { $0.count >= 5 && $0[3] == parent && $0[4] != "conductor" }.sorted { $0[0] < $1[0] }
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

func ctxColor(_ c) -> String {                              // by REMAINING context threshold
    let d = Double(c)
    if d == nil { return "#6F6E77" }
    if d > 50 { return "#30A46C" }                          // green  — plenty
    if d > 30 { return "#F5A623" }                          // amber  — watch
    return "#E5484D"                                        // red    — recycle soon
}
func metaText(_ a) -> String {                              // model · effort (fields 7,8)
    if a.count < 9 { return "" }
    let m = (a[7] == "-" || a[7] == "") ? "" : a[7]
    let e = a[8]
    if m == "" { return e }
    if e == "" { return m }
    return "\(m) · \(e)"
}
func ctxRow(_ a) -> some View {
    if a[2] == "-" { return AnyView(EmptyView()) }           // no ctx (e.g. codex/pending) -> no bar
    let d = Double(a[2])
    if d == nil { return AnyView(EmptyView()) }
    let frac = d / 100.0
    return AnyView(HStack(spacing: 7) {
        HStack(spacing: 0) {                                // fill pushed RIGHT by the Spacer -> drains left→right
            Spacer()
            RoundedRectangle(cornerRadius: 3).foregroundColor(ctxColor(a[2])).frame(width: 88 * frac, height: 6)
        }
        .frame(width: 88, height: 6)                         // hard-clamp height — the shape's intrinsic size inflates the row
        .background { RoundedRectangle(cornerRadius: 3).foregroundColor("#2A2E37") }
        Text("\(a[2])%").font(.system(size: 11, design: .monospaced)).foregroundColor(.secondary)
        Spacer()
        Text(metaText(a)).font(.system(size: 10, design: .monospaced)).foregroundColor("#7A7A85").lineLimit(1)
    }.frame(height: 13))
}
func cwdLine(_ a) -> some View {                             // working dir (field 9)
    if a.count < 10 { return AnyView(EmptyView()) }
    if a[9] == "-" || a[9] == "" { return AnyView(EmptyView()) }
    return AnyView(HStack(spacing: 4) {
        Image(systemName: "folder").font(.system(size: 9)).foregroundColor("#6F6E77")
        Text(a[9]).font(.system(size: 10, design: .monospaced)).foregroundColor("#7A7A85").lineLimit(1).truncationMode(.middle)
    })
}
func toolIcon(_ a) -> some View {                            // tool (field 6) — small SF Symbol, no box.
    if a.count < 7 { return AnyView(EmptyView()) }           // only mark non-claude (claude is the default)
    if a[6] == "codex" {
        return AnyView(Image(systemName: "chevron.left.forwardslash.chevron.right")
            .font(.system(size: 10)).foregroundColor("#D0A46C"))
    }
    return AnyView(EmptyView())
}
func lastLine(_ a) -> some View {                            // latest message (field 10)
    if a.count < 11 { return AnyView(EmptyView()) }
    if a[10] == "" { return AnyView(EmptyView()) }
    return AnyView(Text(a[10]).font(.system(size: 12)).foregroundColor(.tertiary).lineLimit(3).truncationMode(.tail))
}

func agentRow(_ a, _ isCon) -> some View {
    return Button(action: { cmux("surface.focus", surface_id: a[5]) }) {
        HStack(alignment: .top, spacing: 7) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Image(systemName: iconFor(a[1])).font(.system(size: isCon ? 13 : 11)).foregroundColor(colorFor(a[1]))
                    Text(a[0])
                        .font(.system(size: isCon ? 15 : 13))
                        .fontWeight(isCon ? .bold : .semibold)
                        .foregroundColor(isCon ? colorFor(a[1]) : "#E8E8EC")
                        .lineLimit(1).truncationMode(.tail)
                    toolIcon(a)
                }
                ctxRow(a)
                cwdLine(a)
                lastLine(a)
            }
            Spacer()
        }
        .padding(5)
        .background { RoundedRectangle(cornerRadius: 6).foregroundColor(isCon ? "#14171E" : "#00000000") }
    }
}

func groupView(_ c) -> some View {
    return VStack(alignment: .leading, spacing: 3) {
        agentRow(c, true)
        VStack(alignment: .leading, spacing: 3) {
            ForEach(Array(childrenOf(c[0]).enumerated()), id: \.offset) { j, kid in
                agentRow(kid, false)
            }
        }.padding(.leading, 10)
    }
}

VStack(alignment: .leading, spacing: 8) {
    HStack {
        Text("⚓ Fleet").font(.system(size: 16)).bold()
        Spacer()
        Text("\(agentList().count)").font(.system(size: 11, design: .monospaced)).foregroundColor(.secondary)
        Text(clock.time).font(.system(size: 11, design: .monospaced)).foregroundColor(.secondary)
    }
    Divider()
    if agentList().count == 0 {
        Text("no fleet data (run: fleet paint)").font(.system(size: 12)).foregroundColor(.secondary)
    }
    ForEach(Array(conductors().enumerated()), id: \.offset) { i, c in
        groupView(c)
    }
    Spacer().frame(height: 20)
}.padding(8)
