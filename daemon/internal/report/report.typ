// A session's report (daemon/phases/13-reports.md). report.json is
// report.Doc; this file is compiled with typst's own fonts only.
#import "@preview/cmarker:0.1.9"

#let data = json("report.json")
#let ink = rgb("#1d2b3a")
#let accent = rgb("#3b6ea8")
#let failed = rgb("#b03a2e")
#let muted = luma(105)
#let faint = luma(244)
#let mono = "DejaVu Sans Mono"

// Model-written markdown: raw-typst off, so nothing in it runs as typst, and
// straight quotes kept, since much of it is code and logs.
#let md(body, prefix) = cmarker.render(
  body,
  raw-typst: false,
  set-document-title: false,
  smart-punctuation: false,
  h1-level: 3,
  label-prefix: prefix,
)

#set document(title: data.title)
#set page(
  paper: "a4",
  margin: (x: 2.1cm, top: 2cm, bottom: 2.2cm),
  footer: context {
    set text(8pt, fill: muted)
    [#data.title #h(1fr) #data.id #h(1fr) #counter(page).display("1 of 1", both: true)]
  },
)
#set text(font: "Libertinus Serif", size: 10.5pt, lang: "en")
#set par(leading: 0.62em, spacing: 0.9em)
#show raw: set text(font: mono, size: 8pt)
#show raw.where(block: true): it => block(fill: faint, inset: 7pt, radius: 3pt, width: 100%, it)
#show heading: set text(fill: ink)
#show link: set text(fill: accent)
#set table(stroke: 0.5pt + luma(195), inset: (x: 6pt, y: 4pt))
#show table.cell.where(y: 0): set text(weight: "bold")

#let stamp(ts) = if ts != "" { text(8pt, fill: muted, ts) }
#let label(name, color, ts) = block(spacing: 0.5em)[
  #text(9pt, weight: "bold", fill: color, name) #h(1fr) #stamp(ts)
]

#let tool(e) = block(spacing: 0.55em, breakable: true)[
  #if e.error { text(fill: failed, "✗") } else { text(fill: accent, "▸") }
  #h(3pt) #text(font: mono, size: 8pt, weight: "bold", e.name)
  #h(4pt) #text(font: mono, size: 8pt, e.summary)
  #if e.output != "" {
    let more = if e.more > 0 { "\n… " + str(e.more) + " more lines" } else { "" }
    block(fill: faint, inset: (x: 7pt, y: 5pt), radius: 3pt, width: 100%, above: 4pt,
      text(font: mono, size: 7pt, fill: luma(60), e.output + more))
  }
]

#let entries(list, prefix) = for (i, e) in list.enumerate() {
  let p = prefix + str(i) + "-"
  if e.type == "user" {
    block(width: 100%, inset: (left: 9pt, rest: 8pt), fill: rgb("#eef3f9"), radius: (right: 3pt),
      stroke: (left: 2.5pt + accent), above: 1.4em, breakable: true)[
      #label("Request", accent, e.ts)
      #md(e.text, p)
    ]
  } else if e.type == "assistant" {
    block(width: 100%, above: 1em, breakable: true)[
      #label("Reply", luma(70), e.ts)
      #md(e.text, p)
    ]
  } else if e.type == "note" {
    block(above: 0.8em, text(8.5pt, fill: muted, style: "italic", e.text))
  } else if e.type == "tool" {
    tool(e)
  } else if e.type == "subagent" {
    block(width: 100%, inset: (left: 10pt, y: 4pt), stroke: (left: 1pt + luma(190)), above: 0.9em, breakable: true)[
      #block(spacing: 0.5em)[#text(9pt, weight: "bold")[Subagent: #e.title] #text(8pt, fill: muted)[(#e.agent)]]
      #entries(e.entries, p)
    ]
  }
}

#text(19pt, weight: "bold", fill: ink, data.title)
#v(-4pt)
#text(9pt, fill: muted)[#data.kind session #raw(data.id)#if data.cwd != "" [ · #raw(data.cwd)]]
#v(2pt)
#grid(
  columns: (auto, 1fr, auto, 1fr),
  column-gutter: 8pt,
  row-gutter: 5pt,
  ..data.facts.map(f => (text(8.5pt, fill: muted, f.at(0)), text(8.5pt, f.at(1)))).flatten(),
)
#line(length: 100%, stroke: 0.5pt + luma(200))

#entries(data.entries, "e")
