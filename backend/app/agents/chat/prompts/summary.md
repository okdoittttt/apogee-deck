---
description: 오래된 대화를 요약하는 프롬프트 (Ollama 처럼 컨텍스트가 작은 모델용)
variables: [previous_summary, transcript]
---
You are condensing the earlier part of a conversation between a user and an AI assistant. The assistant will continue the conversation using only your summary and the most recent messages, so anything you leave out is forgotten.

Summary of the conversation so far:
<summary>
{{ previous_summary }}
</summary>

Messages to fold into the summary:
<transcript>
{{ transcript }}
</transcript>

Write an updated summary that fully replaces the previous one. Keep, starting with what the user told you about themselves:
- what the user told you about themselves: name, preferences, constraints, goals
- decisions made and conclusions reached
- questions and tasks that are still open
- specific names, numbers, dates, and other details that may be referred to later

Drop greetings, small talk, and details that no longer matter. Do not add anything that is not in the conversation. Keep relative dates and times (such as "next month") exactly as they were said; do not turn them into specific dates unless the conversation itself states the specific date.

Write the summary in the same language the conversation is in, as concise bullet points, in at most about 250 words. When space runs short, shorten details of past topics rather than facts about the user. Output only the summary.
