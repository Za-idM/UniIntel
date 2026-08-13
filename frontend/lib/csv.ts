// Minimal client-side CSV parsing for the upload preview only -- the real
// parse happens server-side (Python csv.DictReader) once the file is
// posted. Handles quoted fields well enough for a preview table.
export function parseCsvPreview(text: string, maxRows = 6): { headers: string[]; rows: string[][] } {
  const lines = text.split(/\r\n|\n|\r/).filter((l) => l.length > 0);
  const parseLine = (line: string): string[] => {
    const cells: string[] = [];
    let cur = "";
    let inQuotes = false;
    for (let i = 0; i < line.length; i++) {
      const ch = line[i];
      if (inQuotes) {
        if (ch === '"' && line[i + 1] === '"') {
          cur += '"';
          i++;
        } else if (ch === '"') {
          inQuotes = false;
        } else {
          cur += ch;
        }
      } else if (ch === '"') {
        inQuotes = true;
      } else if (ch === ",") {
        cells.push(cur);
        cur = "";
      } else {
        cur += ch;
      }
    }
    cells.push(cur);
    return cells;
  };

  if (lines.length === 0) return { headers: [], rows: [] };
  const headers = parseLine(lines[0]);
  const rows = lines.slice(1, 1 + maxRows).map(parseLine);
  return { headers, rows };
}
