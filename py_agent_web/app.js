const fileInput = document.getElementById("fileInput");
const fileNameInput = document.getElementById("fileName");
const rawText = document.getElementById("rawText");
const ingestBtn = document.getElementById("ingestBtn");
const statusEl = document.getElementById("status");
const summaryEl = document.getElementById("summary");
const flagsEl = document.getElementById("flags");
const questionInput = document.getElementById("question");
const askBtn = document.getElementById("askBtn");
const chatEl = document.getElementById("chat");

const mTotalValue = document.getElementById("mTotalValue");
const mTotalCost = document.getElementById("mTotalCost");
const mTotalPnl = document.getElementById("mTotalPnl");
const mHoldings = document.getElementById("mHoldings");
rawText.value = `ticker,quantity,purchase_price,current_price
RELIANCE.NS,120,2825.4,2980.0
TCS.NS,20,3250.0,3580.4
HDFCBANK.NS,50,1600,1540`;
fileNameInput.value = "demo_portfolio.csv";

fileInput.addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  rawText.value = await file.text();
  fileNameInput.value = file.name;
  statusEl.textContent = `Loaded ${file.name}.`;
});

ingestBtn.addEventListener("click", async () => {
  statusEl.textContent = "Analyzing...";
  const res = await fetch("/api/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fileName: fileNameInput.value || "statement.csv", rawText: rawText.value }),
  });
  const data = await res.json();

  if (!res.ok || !data.ok) {
    statusEl.textContent = `Ingest failed: ${data.error || "unknown"}`;
    return;
  }

  summaryEl.textContent = data.summary_text || "No summary";
  flagsEl.innerHTML = "";
  (data.flags || []).forEach((f) => {
    const li = document.createElement("li");
    li.textContent = `${f.icon} ${f.text}`;
    flagsEl.appendChild(li);
  });
  statusEl.textContent = `✅ Ingested ${data.fileName} (${data.holdings_count} holdings).`;
  chatEl.innerHTML = "";
});

const totals = data.analysis?.totals || {};
mTotalValue.textContent = totals.total_value != null ? `₹${Number(totals.total_value).toLocaleString()}` : "—";
mTotalCost.textContent = totals.total_cost != null ? `₹${Number(totals.total_cost).toLocaleString()}` : "—";
mTotalPnl.textContent = totals.total_pnl != null ? `₹${Number(totals.total_pnl).toLocaleString()}` : "—";
mHoldings.textContent = `${totals.priced_holdings ?? 0}/${totals.holdings ?? 0}`;
function addChat(role, text) {
  const box = document.createElement("div");
  box.className = `msg ${role}`;
  box.innerHTML = `<strong>${role === "user" ? "You" : "Agent"}</strong><div>${text.replaceAll("\n", "<br>")}</div>`;
  chatEl.appendChild(box);
}

async function ask() {
  const q = questionInput.value.trim();
  if (!q) return;
  questionInput.value = "";
  addChat("user", q);

  const res = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question: q }),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    addChat("assistant", `⚠️ ${data.error || "Unable to answer"}`);
    return;
  }
  addChat("assistant", data.answer || "No answer");
}

askBtn.addEventListener("click", ask);
questionInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    ask();
  }
});
