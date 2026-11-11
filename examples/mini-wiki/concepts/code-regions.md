---
title: Code Regions
category: concept
tags: [code-regions, wiki-syntax, knuth]
source_theorist: donald-knuth
ontology_mappings: []
---

# Code Regions

A code region is a span of a wiki page the author intends to be read as
literal text rather than prose: fenced blocks, indented blocks, and inline
code. The link checker exempts wikilinks inside these regions because a link
written inside an example is a quotation, not a reference — the argument is
made in [[algorithms]] under "Pseudocode is a quotation".

Exemption is not silence: every skipped link is counted under
`code_block_exempt` so the corpus can still be audited. The distinction
matters to graph health the same way the choice of representation matters to
[[computational-complexity]] — the measure must describe the corpus, not the
author's examples. Wiki syntax has no ontology entity of its own, so this
page carries an empty `ontology_mappings` list on purpose.

Tilde-fenced block:

~~~text
[[tilde-fenced-example]]
~~~

Indented by four spaces (preceded by a blank line, so CommonMark reads it as
a code block rather than a paragraph continuation):

    [[indented-example]]

And inline code: `[[inline-example]]` is likewise exempt.

This page deliberately keeps one wikilink of each of the three kinds, all
quoting page names that do not exist; the two real links above are the only
ones the graph should count from here.
