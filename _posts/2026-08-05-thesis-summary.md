---
layout: post
title: "Explaining my PhD thesis"
date: 2026-08-05
---

### Outline

LLMs are good at predicting the next word. Does that mean they process language similarly to humans?

- Do LLMs have the ability to learn a new language from grammar rules, like humans do?

<!-- link paper -->
Not really: LLMs learn better from unstructured parallel data, rather than descriptive grammar rules.

- Do LLMs reason about translation in a similar way to humans?

<!-- link paper -->
No: LLMs don't benefit from an explicit decomposition step, preferring simple self-refinement.

- Are formal (metalinguistic) and functional (linguistic) competences dissociated in LLMs, like in humans?

<!-- link paper -->
There is an _asymmetric_ association: language analysis (exemplified as glossing) depends on language use (exemplified as translation).

- Do LLMs with metalinguistic reasoning abilities also have strong linguistic abilities e.g. in translation, like for humans?

<!-- describe next work -->
Let's see!

- Should we really expect _a priori_ LLMs to be good models of human neuro-organisation?

<!-- describe position -->
TBC.

### Related Works

- Metalinguistic knowledge is rich: how can we best use it with LLMs for translating new languages?

Applying reasoning to generate rationales and fine-tuning a smaller model on these outperforms long in-context learning with grammar rules.
