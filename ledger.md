---
description: Automatically write the planning blueprint directly into a physical workspace file
---
We are concluding our architectural planning phase. I need you to use your file-writing tools to create a physical file named '(repository being worked on)_plan.md' in the root directory of this workspace. 

Inside 'plan.md', document our entire session history (or from last ledger if this command has been used before in the session) into a dense, high-fidelity Markdown Implementation Ledger. Do not write introductory text in the file; start directly with the ledger. Ensure you strictly document:

1. **System & Tech Stack:** Active frameworks, exact versions, and environment constraints.
2. **The Blueprint:** Complete file directory layout tree showing exactly where new files go or which existing files must be modified.
3. **Strict Code Contracts:** Exact variable names, function signatures, types, API schemas, and return types agreed upon during planning.
4. **Sequential Implementation Steps:** A punchy, numbered checklist of logical steps the build agent must take to execute the build. Group steps logically.
5. **Edge Cases & Logic Traps:** Any explicit constraints, security rules, or architectural landmines we identified that must be avoided.

Once you have successfully written and saved the file to the disk, reply in the chat window with a single confirmation string: '[HANDOFF] plan.md written successfully.'
