const $ = (q)=>document.querySelector(q);

async function loadSessions(){
  const res = await fetch("/api/sessions");
  const data = await res.json();
  const ul = $("#sessions"); ul.innerHTML="";
  data.forEach(s=>{
    const li = document.createElement("li");
    li.className="session-item";
    li.textContent = `${s.session_id} — ${s.job_title||"Untitled"} (${s.status||"draft"})`;
    li.onclick=()=>showSession(s.session_id);
    ul.appendChild(li);
  });
}

async function showSession(id){
  const res = await fetch(`/api/sessions/${id}`);
  const s = await res.json();
  const div = $("#sessionDetail");
  const quoteRows = (s.quote||[]).map((it,i)=>`
    <tr>
      <td><input data-i="${i}" data-k="description" type="text" value="${it.description||""}"/></td>
      <td><input data-i="${i}" data-k="quantity" type="text" value="${it.quantity||1}"/></td>
      <td><input data-i="${i}" data-k="unit_price" type="text" value="${it.unit_price||0}"/></td>
      <td>${Number(it.quantity||1)*Number(it.unit_price||0)}</td>
    </tr>`).join("");

  div.innerHTML = `
    <div class="kv">
      <div>Session:</div><div>${s.session_id}</div>
      <div>Client:</div><div><input id="client_name" type="text" value="${s.client_name||""}"/></div>
      <div>Job Title:</div><div><input id="job_title" type="text" value="${s.job_title||""}"/></div>
      <div>Status:</div><div><input id="status" type="text" value="${s.status||"draft"}"/></div>
      <div>Notes:</div><div><textarea id="notes" rows="3">${s.notes||""}</textarea></div>
    </div>

    <h3>Quote</h3>
    <div>
      <button class="btn" id="aiQuote">Use AI Quote</button>
      <button class="btn" id="save">Save</button>
      <a class="btn" href="/api/sessions/${s.session_id}/export/html">Export HTML</a>
      <a class="btn" href="/api/sessions/${s.session_id}/export/pdf">Export PDF</a>
      <a class="btn" href="/api/sessions/${s.session_id}/calendar.ics">Add to Calendar</a>
    </div>

    <table>
      <thead><tr><th>Description</th><th>Qty</th><th>Unit</th><th>Total</th></tr></thead>
      <tbody id="qbody">${quoteRows}</tbody>
    </table>

    <h4>Transcripts</h4>
    <pre>${(s.transcripts||[]).map(t=> (t.text?("• "+t.text):("• ERROR: "+t.error))).join("\n")}</pre>
  `;

  $("#aiQuote").onclick = async ()=>{
    const r = await fetch(`/api/sessions/${s.session_id}/suggest_quote`,{method:"POST"});
    if(!r.ok){ alert("AI quote failed"); return; }
    showSession(s.session_id);
  };

  $("#save").onclick = async ()=>{
    const rows = [...document.querySelectorAll("#qbody tr")];
    const items = rows.map(tr=>{
      const ins = tr.querySelectorAll("input");
      const obj = {};
      ins.forEach(i=> obj[i.dataset.k] = i.value);
      obj.quantity = Number(obj.quantity||1); obj.unit_price = Number(obj.unit_price||0);
      return obj;
    });
    const body = {
      client_name: $("#client_name").value,
      job_title: $("#job_title").value,
      status: $("#status").value,
      notes: $("#notes").value,
      quote: items,
      quote_total: items.reduce((a,b)=> a + (Number(b.quantity||1)*Number(b.unit_price||0)), 0)
    };
    const r = await fetch(`/api/sessions/${s.session_id}`,{
      method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify(body)
    });
    if(r.ok){ alert("Saved"); loadSessions(); }
  };
}

async function handleUpload(inputId, url){
  const el = $(inputId);
  el.addEventListener("change", async ()=>{
    const file = el.files[0]; if(!file) return;
    const fd = new FormData(); fd.append("file", file);
    const res = await fetch(url,{ method:"POST", body: fd });
    const data = await res.json();
    $("#uploadStatus").textContent = JSON.stringify(data,null,2);
    loadSessions();
  });
}

handleUpload("#photo", "/api/upload/photo");
handleUpload("#audio", "/api/upload/audio");
loadSessions();
