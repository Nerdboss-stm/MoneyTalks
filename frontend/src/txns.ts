import jul from "../../shared/explain/transactions_jul.csv?raw";
import aug from "../../shared/explain/transactions_aug.csv?raw";

/* The two seeded transaction ledgers, bundled so the evidence drawer resolves txn_ids with the network off. */
export interface Txn {
  id: string;
  date: string;
  customer: string;
  vendor: string;
  account: string;
  amount_cents: number;
  owner: string;
}

let index: Map<string, Txn> | null = null;

function fields(line: string): string[] {
  const out: string[] = [];
  let cur = "";
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quoted) {
      if (c === '"' && line[i + 1] === '"') {
        cur += '"';
        i++;
      } else if (c === '"') quoted = false;
      else cur += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") {
      out.push(cur);
      cur = "";
    } else cur += c;
  }
  out.push(cur);
  return out;
}

function parse(csv: string, into: Map<string, Txn>): void {
  const lines = csv.split(/\r?\n/).filter(Boolean);
  if (!lines.length) return;
  const head = fields(lines[0]);
  const col = (name: string) => head.indexOf(name);
  const [iId, iDate, iCust, iVendor, iAcct, iAmt, iOwner] = ["txn_id", "date", "customer", "vendor", "account", "amount_cents", "owner_agent"].map(col);
  for (let i = 1; i < lines.length; i++) {
    const f = fields(lines[i]);
    const id = f[iId];
    if (!id) continue;
    into.set(id, { id, date: f[iDate] ?? "", customer: f[iCust] ?? "", vendor: f[iVendor] ?? "", account: f[iAcct] ?? "", amount_cents: Number(f[iAmt] ?? 0) || 0, owner: f[iOwner] ?? "" });
  }
}

export function txn(id: string): Txn | undefined {
  if (!index) {
    index = new Map();
    parse(jul, index);
    parse(aug, index);
  }
  return index.get(id);
}
