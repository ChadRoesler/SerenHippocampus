"""
seren_hippocampus.mcp
═════════════════════

Optional MCP surface for SerenHippocampus. Only meaningful when the [mcp]
extra is installed (`pip install seren-hippocampus[mcp]`); without it this
subpackage's modules fail to import and app.py's mount attempt no-ops,
leaving the service in pure-HTTP mode.

WHY THIS EXISTS: the reviewer of every draft is the main model, in a session.
It could already see the drafts (Memory's list_drafts / review_draft) and
write the brief that opens a sleep (Memory's submit_brief), but it could not
see the sleep itself - did last night's run fail, is a chain still open, when
is bedtime, is the model even up - or say "sleep now, don't wait for the
tick". Those lived only on this service's HTTP routes and the viewer, which a
session does not reach. Same reason Memory, Loci and Margin each grew one.

The tools call the in-process Hippocampus and MemoryClient the routes use
(app.state.hippocampus / app.state.memory), not HTTP to ourselves: one lock,
one state file, one set of logs, and a Busy from a sleep the loop started is
the same Busy a tool sees.
"""
