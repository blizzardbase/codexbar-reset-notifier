---
name: Feature request
about: Suggest a change to how the notifier behaves
title: ''
labels: enhancement
assignees: ''
---

## The problem

<!-- What are you trying to do that this project makes hard or impossible? -->

## What you would like instead

<!-- Describe the behavior, not the implementation, if you can. -->

## Alternatives you considered

<!-- Including "do nothing" — sometimes the answer is that the current behavior is right. -->

## Constraint check

This project keeps a deliberately narrow scope. Please confirm your request fits:

- [ ] It needs no third-party Python package
- [ ] It needs no AI, LLM, or paid API call
- [ ] It does not require hard-coding a reset interval
- [ ] It does not require an inbound port on the VPS, `sudo`, or writes outside `vps_remote_dir`
- [ ] It does not put a secret anywhere other than `.env`

If your request does not fit, say so anyway and explain why it is worth the exception.

## Notification impact

Does this change what the Telegram message says, or how often it arrives? The project sends **exactly one message per Claude session reset** by design — no warnings, no separate Codex message.

<!-- yes / no, and how -->
