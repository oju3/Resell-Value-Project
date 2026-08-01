# CLAUDE.md

- After completing any task that involves tool use (running commands, editing files, database operations), end your response with a brief summary of the work done: what changed, what was created or modified, and anything I should be aware of.
- This is a sneaker resale valuation app: FastAPI backend, Supabase Postgres (connection in .env), React frontend later. Sneakers are treated like traded assets.
- Never print, log, or commit the contents of .env. It is gitignored and must stay that way.
- Bias toward action: implement changes rather than only suggesting them. For small ambiguities (naming, file organization, minor implementation details), infer the most useful action and proceed — use tools to check actual files/data rather than guessing. But for decisions that affect the spec, schema, data integrity, or financial calculations, ask before proceeding. When you do infer, state the assumption you made in your summary.
- Prefer simple, explainable implementations over clever ones — I'm learning and will need to explain this code in interviews.
- After completing a working feature, commit and push with a clear commit message describing what was built.
- Never add Co-Authored-By trailers to commit messages.
- When making multiple tool calls with no dependencies between them, run them in parallel rather than sequentially (e.g., reading 3 files = 3 parallel reads). If a call depends on a previous call's result for its parameters, run those sequentially. Never use placeholders or guess missing parameters in a tool call — discover real values first.
- Never speculate about code you haven't opened. If a specific file is referenced, read it before answering. Investigate relevant files before answering questions about the codebase. Don't make claims about code without checking unless completely certain — give grounded, hallucination-free answers.
