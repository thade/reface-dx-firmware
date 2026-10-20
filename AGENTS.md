# Writing in this repository

The README is read by reface DX owners, not reviewers. Write the way its
"Using it" section does: plain sentences, one idea each, in the order the
reader needs. The reverse-engineering notes under `docs/` can be technical,
but the same habits apply.

## Patterns to avoid

- A colon that sets up a punchline. "Nothing is uploaded: the page does it
  all." Write two sentences, or join them with "because" or "so".
- A clipped fragment after a colon. "CLOCK off: silence." Say what
  happens: "When CLOCK is off, the DX sends no clock."
- A reassurance tacked on the end. "...exactly as before", "...same as
  always", "the DX remembers your choice". State the fact once, plainly,
  or drop it.
- A reveal. "...and that is the switch." Say what to do: "To turn the clock
  off, tap the leftmost DATA ENTRY switch."
- "Simply", "just", "seamless", "robust", "powerful", "ensure". Cut them.
- Three short sentences in a row for rhythm. Vary the length because the
  content varies, not for effect.
- "Do X and you Y." Prefer "When X, Y" or "To Y, do X."
- Stating the same rule in two places so each reads complete. State it
  once where it belongs and let the other place refer to it.
- A heading with a trailing qualifier: "A clock switch, persistent". Keep
  the noun; put the qualifier in the text.
- Bold on a phrase mid-sentence. Bold is for the lead-in of a paragraph or
  bullet, and for button names.

## Habits that help

- Name the thing the reader presses, sees or copies, in the instrument's
  own words: FUNCTION, DATA ENTRY, MIDI 2, CLOCK, "reface DX Update".
- Give the fact, then the reason, then the exception, in that order.
- Say what was tested and what was not, with the date, and no more.
- Read the paragraph aloud. If a sentence sounds like a slogan, it is.
- Facts come from the code, the emulation suites and the hardware record in
  `docs/hardware-tests.md`, not from an earlier draft of the prose.
- Addresses and byte values belong in `docs/`, not in the README.
