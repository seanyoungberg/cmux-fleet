// fleet.swift — a custom cmux sidebar that renders the live fleet as a board.
//
// Install:
//     mkdir -p ~/.config/cmux/sidebars
//     cp sidebars/fleet.swift ~/.config/cmux/sidebars/fleet.swift
//     cmux sidebar validate fleet && cmux sidebar open fleet     # or: cmux sidebar select fleet
//
// It binds to cmux's LIVE `workspaces` context (refreshes ~1s). `fleet paint` writes each agent's
// context-remaining as a sidebar progress bar (set-progress) and a state pill (set-status) onto its
// workspace; this sidebar reads the native `progress` field back plus `latestMessage`, so the board
// stays correct on its own. Rows are tappable to jump. A true PARENTAGE tree isn't in the workspace
// binding (cmux exposes no parent field) — use `fleet graph` / `fleet serve` for the tree; this is the
// at-a-glance board. Keep `fleet paint` running (re-run, or from the router) to keep the bars fresh.
VStack(alignment: .leading, spacing: 8) {
    HStack {
        Text("⚓ Fleet").font(.title3).bold()
        Spacer()
        Text(clock.time).font(.caption).foregroundColor(.secondary).monospacedDigit()
    }
    Text("\(workspaceCount) workspaces · \(unreadTotal) unread")
        .font(.caption).foregroundColor(.secondary)
    Divider()
    ScrollView {
        LazyVStack(alignment: .leading, spacing: 6) {
            ForEach(workspaces) { w in
                Button(action: { cmux("workspace.select", workspace_id: w.id) }) {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(w.selected ? "●" : "○")
                                .foregroundColor(w.selected ? "#FFD24A" : .secondary)
                            Text(w.title).bold().lineLimit(1)
                            Spacer()
                            w.unread > 0 ? Text("\(w.unread)").font(.caption2)
                                .foregroundColor("#F5A623") : nil
                        }
                        // our context-remaining bar (written by `fleet paint` via set-progress)
                        w.progress != nil
                            ? AnyView(HStack(spacing: 6) {
                                ProgressView(value: w.progress!.value).frame(maxWidth: 90)
                                Text(w.progress!.label).font(.caption2).foregroundColor(.secondary)
                              })
                            : AnyView(EmptyView())
                        w.latestMessage != nil
                            ? AnyView(Text(w.latestMessage!).font(.caption2)
                                .foregroundColor(.secondary).lineLimit(1).truncationMode(.tail))
                            : AnyView(EmptyView())
                    }
                    .padding(6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(w.selected ? "#16181D" : "#0E1117")
                    .cornerRadius(8)
                }
            }
        }
    }
}
.padding(10)
