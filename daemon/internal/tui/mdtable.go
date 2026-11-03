package tui

import (
	"strconv"
	"strings"
)

// Pipe tables are rebuilt with column widths in terminal cells, and a table too wide for
// the room becomes "Header: value" blocks.

// A column is at least 3 cells wide, matching the divider's minimum dash run
// (three dashes).
const minColWidth = 3

func splitTableRow(row string) []string {
	s := strings.TrimSpace(row)
	s = strings.TrimPrefix(s, "|")
	s = strings.TrimSuffix(s, "|")
	cells := strings.Split(s, "|")
	for i := range cells {
		cells[i] = strings.TrimSpace(cells[i])
	}
	return cells
}

func isDividerCell(c string) bool {
	c = strings.TrimSpace(c)
	c = strings.TrimPrefix(c, ":")
	c = strings.TrimSuffix(c, ":")
	return len(c) >= 3 && strings.Trim(c, "-") == ""
}

func isTableDivider(row string) bool {
	cells := splitTableRow(row)
	if len(cells) <= 1 {
		return false
	}
	for _, c := range cells {
		if !isDividerCell(c) {
			return false
		}
	}
	return true
}

func padTo(s string, target int) string {
	return s + strings.Repeat(" ", max(0, target-textWidth(s)))
}

// renderTable draws header and body rows at uniform widths; avail < 0 means
// any width.
func renderTable(rows [][]string, avail int) []string {
	ncols := 0
	for _, r := range rows {
		ncols = max(ncols, len(r))
	}
	for i := range rows {
		for len(rows[i]) < ncols {
			rows[i] = append(rows[i], "")
		}
	}
	widths := make([]int, ncols)
	total := 1
	for c := range widths {
		widths[c] = minColWidth
		for _, r := range rows {
			widths[c] = max(widths[c], textWidth(r[c]))
		}
		total += widths[c] + 3
	}
	if avail >= 0 && total > max(avail, 20) {
		return renderVertical(rows, ncols, avail)
	}
	row := func(cells []string) string {
		parts := make([]string, len(cells))
		for k, c := range cells {
			parts[k] = padTo(c, widths[k])
		}
		return "| " + strings.Join(parts, " | ") + " |"
	}
	out := []string{row(rows[0])}
	dashes := make([]string, ncols)
	for k, w := range widths {
		dashes[k] = strings.Repeat("-", w+2)
	}
	out = append(out, "|"+strings.Join(dashes, "|")+"|")
	for _, r := range rows[1:] {
		out = append(out, row(r))
	}
	return out
}

// wrapToWidth soft-wraps text at word boundaries to width cells, breaking a
// word wider than that.
func wrapToWidth(text string, width int) []string {
	if width <= 0 || text == "" {
		return []string{text}
	}
	words := strings.Fields(text)
	if len(words) == 0 {
		return []string{""}
	}
	var lines []string
	cur, curW := "", 0
	hardBreak := func(word string) []string {
		var out []string
		buf, bw := "", 0
		for _, ch := range word {
			cw := max(runeWidth(ch), 1)
			if bw+cw > width && buf != "" {
				out = append(out, buf)
				buf, bw = string(ch), cw
			} else {
				buf += string(ch)
				bw += cw
			}
		}
		if buf != "" {
			out = append(out, buf)
		}
		return out
	}
	start := func(word string) {
		ww := textWidth(word)
		if ww <= width {
			cur, curW = word, ww
			return
		}
		pieces := hardBreak(word)
		lines = append(lines, pieces[:len(pieces)-1]...)
		cur = pieces[len(pieces)-1]
		curW = textWidth(cur)
	}
	for _, word := range words {
		ww := textWidth(word)
		switch {
		case cur == "":
			start(word)
		case curW+1+ww <= width:
			cur += " " + word
			curW += 1 + ww
		default:
			lines = append(lines, cur)
			start(word)
		}
	}
	if cur != "" {
		lines = append(lines, cur)
	}
	if len(lines) == 0 {
		return []string{""}
	}
	return lines
}

// renderVertical draws a too-wide table as "Header: value" blocks, a thin rule
// between rows. A value whose first word does not fit beside its label starts
// on the line below, indented.
func renderVertical(rows [][]string, ncols, avail int) []string {
	if len(rows) == 0 {
		return nil
	}
	headers := rows[0]
	labels := make([]string, ncols)
	for i := range labels {
		labels[i] = headers[i]
		if labels[i] == "" {
			labels[i] = "Column " + itoa(i+1)
		}
	}
	sepW := 30
	if avail > 0 {
		sepW = max(20, min(40, avail-2))
	}
	sep := strings.Repeat("─", sepW)
	const indent = "  "
	contBudget := max(10, avail-len(indent))
	var out []string
	for ri, row := range rows[1:] {
		if ri > 0 {
			out = append(out, sep)
		}
		for ci := 0; ci < ncols; ci++ {
			label, value := labels[ci], row[ci]
			words := strings.Fields(value)
			if len(words) == 0 {
				out = append(out, label+":")
				continue
			}
			first := avail - textWidth(label) - 2
			if textWidth(words[0]) > first {
				out = append(out, label+":")
				for _, l := range wrapToWidth(value, contBudget) {
					if strings.TrimSpace(l) != "" {
						out = append(out, indent+l)
					}
				}
				continue
			}
			line, lw, i := words[0], textWidth(words[0]), 1
			for i < len(words) && lw+1+textWidth(words[i]) <= first {
				line += " " + words[i]
				lw += 1 + textWidth(words[i])
				i++
			}
			out = append(out, label+": "+line)
			if i < len(words) {
				for _, l := range wrapToWidth(strings.Join(words[i:], " "), contBudget) {
					if strings.TrimSpace(l) != "" {
						out = append(out, indent+l)
					}
				}
			}
		}
	}
	return out
}

func itoa(n int) string { return strconv.Itoa(n) }

// realignMarkdownTables rewrites every table block of text; lines that are not
// part of a table pass through. avail < 0 means any width.
func realignMarkdownTables(text string, avail int) string {
	if !strings.Contains(text, "|") {
		return text
	}
	lines := strings.Split(text, "\n")
	var out []string
	for i := 0; i < len(lines); {
		line := lines[i]
		if strings.Contains(line, "|") && i+1 < len(lines) && isTableDivider(lines[i+1]) {
			rows := [][]string{splitTableRow(line)}
			j := i + 2
			for j < len(lines) && strings.Contains(lines[j], "|") && strings.TrimSpace(lines[j]) != "" {
				if !isTableDivider(lines[j]) {
					rows = append(rows, splitTableRow(lines[j]))
				}
				j++
			}
			hasCell := false
			for _, c := range rows[0] {
				hasCell = hasCell || c != ""
			}
			if hasCell || len(rows) > 1 {
				out = append(out, renderTable(rows, avail)...)
				i = j
				continue
			}
		}
		out = append(out, line)
		i++
	}
	return strings.Join(out, "\n")
}
