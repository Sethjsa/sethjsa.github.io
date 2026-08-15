---
layout: post
title: "PhD thesis, briefly"
date: 2026-08-05
---

### Outline

LLMs are good at translating between languages. Does that mean they process languages similarly to humans?

1. Do LLMs have the ability to learn a new language from grammar rules, like humans do?

   Not really: LLMs learn better from unstructured parallel data, rather than descriptive grammar rules. [[paper]](https://openreview.net/forum?id=aMBSY2ebPw)

2. Do LLMs reason about translation in a similar way to humans?

   No: LLMs don't benefit from an explicit decomposition step, preferring simple self-refinement. [[paper]](https://aclanthology.org/2025.emnlp-main.1031/)

3. Are formal (metalinguistic) and functional (linguistic) competences dissociated in LLMs, like in humans?

   There is an _asymmetric_ association: language analysis (exemplified as glossing) depends on language use (exemplified as translation). (Under submission)

4. Do LLMs with metalinguistic reasoning abilities also have strong linguistic abilities e.g. in translation, like for humans?

   Let's see!

5. Should we really expect _a priori_ LLMs to be good models of human neuro-organisation and translation abilities?

   TBC

### Related Works

- Metalinguistic knowledge is rich: how can we best use it with LLMs for translating new languages?

  Applying reasoning to generate rationales and fine-tuning a smaller model on these outperforms long in-context learning with grammar rules. (Under submission)

- Metalinguistic knowledge is difficult to learn: can we measure how well LLMs can induce it from unlabelled text?

  LLMs perform poorly on typological feature induction, especially after correcting for data contamination. (In progress)

- Grammar in-context is an imperfect, cheap method for adding language capabilities: at the pre-training side, can merging separately pre-trained language-specific models instead, without joint multilingual pre-training?

  No: merging separately pre-trained monolingual models causes performance to collapse from interference, lacking shared representations. Is there no free lunch for adding languages in pre-training and in-context?

