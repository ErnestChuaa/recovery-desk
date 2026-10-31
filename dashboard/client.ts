// Framework-independent adapter. Set API to the backend address.
// A working reference dashboard (timeline, negotiation trace, review card) is served at GET /ui.
export const API = "http://localhost:8000";
export async function request(path: string, method = "GET", body?: unknown) {
  const r = await fetch(API + path, { method, headers: {"Content-Type":"application/json"},
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  return r.json();
}
export const getState = () => request("/state");
export const getBaseline = () => request("/baseline");
export const loadDotRow = (row: number) => request("/scenario/from-csv", "POST", {row});   // needs FLIGHTS_CSV on the server
export const suggestRows = (n = 10) => request(`/scenario/suggest?n=${n}`);
export const startRun = (mode: "fixed" | "claude" = "fixed") => request("/runs", "POST", {mode});
export const getRun = (id: string) => request(`/runs/${id}`);
export const approveRun = (id: string) => request(`/runs/${id}/approve`, "POST");
export const disrupt = (event: unknown) => request("/disruptions", "POST", event);
export function subscribe(onEvent: (event: any) => void, after = 0) {
  const stream = new EventSource(`${API}/events/stream?after=${after}`);
  stream.onmessage = e => onEvent(JSON.parse(e.data));
  return () => stream.close();
}
