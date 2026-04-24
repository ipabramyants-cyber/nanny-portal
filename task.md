# Task: Fix app.py syntax errors + push to Railway

## Problem
app.py has truncated functions from previous session edits.
Each create_app() block has:
1. `api_client_date_action` truncated at `_d_re = r'^[0-9]{4}-[0-9]{2}-[0-9]{2}\n`
2. `api_client_add_dates` truncated similarly
3. Function bodies orphaned AFTER `if __name__ == '__main__':` block

## Structure
- app.py has 11 `if __name__ == '__main__':` blocks (one per mode variant)
- Each block has orphaned code after it  
- The truncated regex is: `_d_re = r'^[0-9]{4}-[0-9]{2}-[0-9]{2}\n` (missing closing `$'` and rest)

## Fix Plan
### Strategy: Python script to fix all occurrences
1. For each `__main__` block, identify orphaned code after it
2. Move that code back into the truncated function above `return app`
3. Fix the truncated regex to be complete: `_d_re = r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$'`

## __main__ at lines:
3063, 4543, 6211, 7682, 9220, 10888, 12349, 14017, 15488, 17026, 18694

## Status
- [ ] Fix app.py
- [ ] Syntax check
- [ ] Git push
