/** @odoo-module **/
// When a call may be finished. Plain functions, no Odoo imports, so the rule
// runs under Node in static/tests. The server refuses the same thing
// (controllers/main.py, MIN_NOTE_CHARS): this only switches Save off early.

export const MIN_NOTE_CHARS = 3;

// A note is written when it has at least MIN_NOTE_CHARS characters besides spaces at the ends.
export function noteOk(note) {
    return (note || "").trim().length >= MIN_NOTE_CHARS;
}
