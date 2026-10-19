// Turn a pasted list or a CSV file into items a task can run on.
//
// The values can identify a patient. Parsing happens in this window only;
// the rows go to the local engine one item at a time and never into logs.

import type { QualificationParameter } from "./types";
import { humanizeInput } from "./outcomes";

export interface ParsedItem {
  position: number;
  values: Record<string, string>;
  /** Plain-language reasons this row can't run. Empty when it can. */
  problems: string[];
}

export interface ParsedItems {
  items: ParsedItem[];
  /** True when the first row named the inputs. */
  header: boolean;
  /** Plain-language problem with the whole paste, if any. */
  error: string | null;
  /** Columns that matched no input and were ignored. */
  ignored: string[];
}

export const MAX_ITEMS = 500;

/** Split delimited text into rows of cells. Handles quoted CSV cells. */
export function splitRows(text: string): string[][] {
  const lines = text.replace(/\r\n?/g, "\n");
  const delimiter = lines.includes("\t")
    ? "\t"
    : /,/.test(lines)
      ? ","
      : null;
  const rows: string[][] = [];
  if (delimiter === null) {
    for (const line of lines.split("\n")) {
      if (line.trim()) rows.push([line.trim()]);
    }
    return rows;
  }
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let index = 0; index < lines.length; index += 1) {
    const char = lines[index];
    if (quoted) {
      if (char === '"') {
        if (lines[index + 1] === '"') {
          cell += '"';
          index += 1;
        } else {
          quoted = false;
        }
      } else {
        cell += char;
      }
      continue;
    }
    if (char === '"' && cell === "") {
      quoted = true;
    } else if (char === delimiter) {
      row.push(cell.trim());
      cell = "";
    } else if (char === "\n") {
      row.push(cell.trim());
      if (row.some((value) => value !== "")) rows.push(row);
      row = [];
      cell = "";
    } else {
      cell += char;
    }
  }
  row.push(cell.trim());
  if (row.some((value) => value !== "")) rows.push(row);
  return rows;
}

function normalize(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function matchInput(
  heading: string,
  inputs: QualificationParameter[],
): QualificationParameter | undefined {
  const wanted = normalize(heading);
  return inputs.find(
    (input) =>
      normalize(input.name) === wanted ||
      normalize(humanizeInput(input.name)) === wanted,
  );
}

/**
 * Map rows to the task's inputs.
 *
 * A first row that names the inputs is a header. Without one, a task with a
 * single input takes one value per line, and a row with exactly one cell per
 * input maps in the order the task lists its inputs.
 *
 * A row with more values than columns, or one that is missing a cell, gets a
 * problem and doesn't run: no value is cut or dropped on the way to the task.
 */
export function parseItems(
  text: string,
  declared: QualificationParameter[],
): ParsedItems {
  const inputs = declared.filter((input) => !input.secret);
  const rows = splitRows(text);
  const empty: ParsedItems = { items: [], header: false, error: null, ignored: [] };
  if (rows.length === 0) return empty;
  if (inputs.length === 0) {
    return {
      ...empty,
      error: "This workflow doesn't take any values, so there's nothing to paste.",
    };
  }

  const first = rows[0];
  const headerMatches = first.map((cell) => matchInput(cell, inputs));
  const header = headerMatches.some(Boolean);
  let columns: (QualificationParameter | undefined)[];
  let body: string[][];
  const ignored: string[] = [];
  if (header) {
    columns = headerMatches;
    body = rows.slice(1);
    first.forEach((cell, index) => {
      if (!headerMatches[index] && cell) ignored.push(cell);
    });
    // An optional input without a column would take the value from the
    // recording on every row. Ask for the column; empty cells are fine.
    const unnamed = inputs.filter(
      (input) => !input.required && !headerMatches.includes(input),
    );
    if (unnamed.length) {
      return {
        ...empty,
        header,
        error:
          "Add a column for " +
          unnamed.map((input) => humanizeInput(input.name)).join(", ") +
          ". Leave its cells empty to enter nothing.",
      };
    }
  } else if (inputs.length === 1) {
    columns = [inputs[0]];
    body = rows;
  } else if (rows.every((row) => row.length === inputs.length)) {
    columns = inputs;
    body = rows;
  } else {
    return {
      ...empty,
      error:
        "Add a first row that names each column: " +
        inputs.map((input) => humanizeInput(input.name)).join(", ") +
        ".",
    };
  }
  if (body.length > MAX_ITEMS) {
    return {
      ...empty,
      header,
      error: `You can run up to ${MAX_ITEMS} items at a time. Split the list and run it in parts.`,
    };
  }

  const items = body.map((row, position) => {
    const values: Record<string, string> = {};
    columns.forEach((input, index) => {
      if (input && row[index] !== undefined) values[input.name] = row[index];
    });
    const problems = inputs
      .filter((input) => input.required && !(values[input.name] ?? "").trim())
      .map((input) => `No ${humanizeInput(input.name)}`);
    // A row never loses a value silently. Cells beyond the columns would be
    // dropped, so "Smith, Jane" in a one-column list would run as "Smith" and
    // the record check would confirm "Smith". Such a row doesn't run.
    if (row.slice(columns.length).some((cell) => cell !== "")) {
      problems.push(
        "This row has more values than columns. If a value has a comma, put it in quotes",
      );
    }
    // A short row is missing a cell for a column the list names. Without the
    // cell, an optional input would take the value from the recording.
    columns.forEach((input, index) => {
      if (input && !input.required && row[index] === undefined) {
        problems.push(`No ${humanizeInput(input.name)}`);
      }
    });
    for (const input of inputs) {
      if (values[input.name] === undefined && !input.required) {
        delete values[input.name];
      }
    }
    return { position, values, problems };
  });
  return { items, header, error: null, ignored };
}
