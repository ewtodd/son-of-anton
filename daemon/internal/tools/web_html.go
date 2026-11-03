package tools

import (
	"bytes"
	"regexp"
	"strconv"
	"strings"
	"unicode"

	"golang.org/x/net/html"
	"golang.org/x/net/html/atom"
	"golang.org/x/net/html/charset"
)

// skipTags are dropped with everything under them.
var skipTags = map[atom.Atom]bool{
	atom.Script: true, atom.Style: true, atom.Noscript: true, atom.Template: true, atom.Svg: true,
	atom.Iframe: true, atom.Form: true, atom.Nav: true, atom.Header: true, atom.Footer: true, atom.Aside: true,
	atom.Head: true,
}

// blockTags set their content off as paragraphs.
var blockTags = map[atom.Atom]bool{
	atom.P: true, atom.Div: true, atom.Section: true, atom.Article: true, atom.Main: true, atom.Figure: true,
	atom.Figcaption: true, atom.Dl: true, atom.Dt: true, atom.Dd: true, atom.Address: true, atom.Details: true,
	atom.Summary: true, atom.Body: true,
}

var blankRuns = regexp.MustCompile(`\n{3,}`)

// htmlToMarkdown reads an HTML page into its title and Markdown text.
func htmlToMarkdown(body []byte, contentType string) (title, md string) {
	r, err := charset.NewReader(bytes.NewReader(body), contentType)
	if err != nil {
		r = bytes.NewReader(body)
	}
	doc, err := html.Parse(r)
	if err != nil {
		return "", ""
	}
	title = pageTitle(doc)
	root := findElement(doc, func(n *html.Node) bool { return n.DataAtom == atom.Article })
	if root == nil {
		root = findElement(doc, func(n *html.Node) bool { return n.DataAtom == atom.Main || attr(n, "role") == "main" })
	}
	if root == nil {
		root = findElement(doc, func(n *html.Node) bool { return n.DataAtom == atom.Body })
	}
	if root == nil {
		root = doc
	}
	c := &mdWriter{}
	c.children(root)
	return title, c.String()
}

// pageTitle is og:title, else the <title>.
func pageTitle(doc *html.Node) string {
	if m := findElement(doc, func(n *html.Node) bool {
		return n.DataAtom == atom.Meta && (attr(n, "property") == "og:title" || attr(n, "name") == "og:title") && strings.TrimSpace(attr(n, "content")) != ""
	}); m != nil {
		return collapseSpace(attr(m, "content"))
	}
	if t := findElement(doc, func(n *html.Node) bool { return n.DataAtom == atom.Title }); t != nil {
		return collapseSpace(textOf(t))
	}
	return ""
}

// findElement is the first element under n, in document order, that match
// accepts.
func findElement(n *html.Node, match func(*html.Node) bool) *html.Node {
	for c := n.FirstChild; c != nil; c = c.NextSibling {
		if c.Type == html.ElementNode && match(c) {
			return c
		}
		if f := findElement(c, match); f != nil {
			return f
		}
	}
	return nil
}

// mdWriter builds Markdown. space is a pending space between inline words, so
// that whitespace collapses and never lands at a line's edge.
type mdWriter struct {
	out   strings.Builder
	space bool
	depth int // list nesting
}

func (c *mdWriter) String() string {
	return strings.TrimSpace(blankRuns.ReplaceAllString(c.out.String(), "\n\n"))
}

func (c *mdWriter) last() byte {
	s := c.out.String()
	if s == "" {
		return '\n'
	}
	return s[len(s)-1]
}

// trimTail drops spaces and tabs at the end of the output.
func (c *mdWriter) trimTail() {
	s := strings.TrimRight(c.out.String(), " \t")
	if len(s) != c.out.Len() {
		c.out.Reset()
		c.out.WriteString(s)
	}
}

// newline ends the current line if there is one.
func (c *mdWriter) newline() {
	c.space = false
	if c.out.Len() == 0 {
		return
	}
	c.trimTail()
	if c.last() != '\n' {
		c.out.WriteByte('\n')
	}
}

// para ends the current paragraph with a blank line.
func (c *mdWriter) para() {
	c.newline()
	if c.out.Len() > 0 && !strings.HasSuffix(c.out.String(), "\n\n") {
		c.out.WriteByte('\n')
	}
}

// word writes inline text, with the pending space before it.
func (c *mdWriter) word(s string) {
	if c.space && c.last() != '\n' && c.last() != ' ' {
		c.out.WriteByte(' ')
	}
	c.space = false
	c.out.WriteString(s)
}

// text writes a text node with its whitespace collapsed.
func (c *mdWriter) text(s string) {
	if s == "" {
		return
	}
	if unicode.IsSpace([]rune(s)[0]) {
		c.space = true
	}
	words := strings.Fields(s)
	for i, w := range words {
		if i > 0 {
			c.space = true
		}
		c.word(w)
	}
	if len(words) > 0 {
		if rs := []rune(s); unicode.IsSpace(rs[len(rs)-1]) {
			c.space = true
		}
	}
}

func (c *mdWriter) children(n *html.Node) {
	for ch := n.FirstChild; ch != nil; ch = ch.NextSibling {
		c.node(ch)
	}
}

func (c *mdWriter) node(n *html.Node) {
	switch n.Type {
	case html.TextNode:
		c.text(n.Data)
		return
	case html.ElementNode:
	default:
		c.children(n)
		return
	}
	a := n.DataAtom
	switch {
	case skipTags[a]:
	case a == atom.H1 || a == atom.H2 || a == atom.H3 || a == atom.H4 || a == atom.H5 || a == atom.H6:
		c.para()
		c.word(strings.Repeat("#", int(n.Data[1]-'0')) + " ")
		c.children(n)
		c.para()
	case blockTags[a]:
		c.para()
		c.children(n)
		c.para()
	case a == atom.Ul || a == atom.Ol:
		c.list(n)
	case a == atom.Pre:
		c.para()
		c.out.WriteString("```\n" + strings.Trim(textOf(n), "\n") + "\n```")
		c.para()
	case a == atom.Code:
		c.word("`")
		c.children(n)
		c.close("`")
	case a == atom.Strong || a == atom.B:
		c.word("**")
		c.children(n)
		c.close("**")
	case a == atom.Em || a == atom.I:
		c.word("*")
		c.children(n)
		c.close("*")
	case a == atom.Blockquote:
		c.para()
		sub := &mdWriter{}
		sub.children(n)
		for line := range strings.SplitSeq(sub.String(), "\n") {
			if line == "" {
				c.out.WriteString(">\n")
			} else {
				c.out.WriteString("> " + line + "\n")
			}
		}
		c.para()
	case a == atom.Br:
		c.newline()
	case a == atom.Table:
		c.para()
		c.table(n)
		c.para()
	case a == atom.Img:
		c.word("[IMAGE: " + collapseSpace(attr(n, "alt")) + "]")
	default:
		c.children(n)
	}
}

// close writes a closing inline marker straight after the text, leaving any
// pending space after it.
func (c *mdWriter) close(marker string) {
	space := c.space
	c.space = false
	c.out.WriteString(marker)
	c.space = space
}

func (c *mdWriter) list(n *html.Node) {
	if c.depth == 0 {
		c.para()
	} else {
		c.newline()
	}
	c.depth++
	i := 0
	for li := n.FirstChild; li != nil; li = li.NextSibling {
		if li.Type != html.ElementNode || li.DataAtom != atom.Li {
			continue
		}
		i++
		c.newline()
		prefix := "- "
		if n.DataAtom == atom.Ol {
			prefix = strconv.Itoa(i) + ". "
		}
		c.out.WriteString(strings.Repeat("  ", c.depth-1) + prefix)
		c.children(li)
		c.newline()
	}
	c.depth--
	if c.depth == 0 {
		c.para()
	}
}

// table writes each row as its cells joined by " | ".
func (c *mdWriter) table(n *html.Node) {
	for ch := n.FirstChild; ch != nil; ch = ch.NextSibling {
		if ch.Type != html.ElementNode {
			continue
		}
		if ch.DataAtom != atom.Tr {
			c.table(ch)
			continue
		}
		var cells []string
		for cell := ch.FirstChild; cell != nil; cell = cell.NextSibling {
			if cell.Type == html.ElementNode && (cell.DataAtom == atom.Td || cell.DataAtom == atom.Th) {
				sub := &mdWriter{}
				sub.children(cell)
				cells = append(cells, collapseSpace(sub.String()))
			}
		}
		if len(cells) > 0 {
			c.newline()
			c.out.WriteString(strings.Join(cells, " | "))
			c.newline()
		}
	}
}
