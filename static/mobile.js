const log = (m)=>{ const el=document.getElementById("log"); el.textContent = (new Date().toLocaleTimeString())+"  "+m+"\n"+el.textContent; };
const serverSpan = document.getElementById("server");

// Auto-detect server base (works when you open by LAN IP)
const BASE = location.origin;
serverSpan.textContent = "server: "+BASE;

const notesEl = document.getElementById("notes");
const photoEl = document.getElementById("photo");
const photosDiv = document.getElementById("photos");
const recBtn = document.getElementById("recBtn");
const stopBtn = document.getElementById("stopBtn");
const preview = document.getElementById("preview");
const sendBtn = document.getElementById("sendBtn");

let audioBlob = null;
let mediaRecorder = null;
let chunks = [];

recBtn.onclick = async ()=>{
  try{
    const stream = await navigator.mediaDevices.getUserMedia({audio:true});
    mediaRecorder = new MediaRecorder(stream, { mimeType: "audio/webm" });
    chunks = [];
    mediaRecorder.ondataavailable = e=> chunks.push(e.data);
    mediaRecorder.onstop = ()=>{
      audioBlob = new Blob(chunks, {type:"audio/webm"});
      preview.src = URL.createObjectURL(audioBlob);
      preview.style.display = "block";
    }
    mediaRecorder.start();
    recBtn.disabled = true; stopBtn.disabled = false;
    log("Recording…");
  }catch(e){ log("Mic error: "+e); }
};

stopBtn.onclick = ()=>{
  if(mediaRecorder && mediaRecorder.state !== "inactive"){
    mediaRecorder.stop();
    recBtn.disabled=false; stopBtn.disabled=true; log("Stopped.");
  }
};

photoEl.onchange = ()=>{
  photosDiv.innerHTML = "";
  [...photoEl.files].forEach(f=>{
    const url = URL.createObjectURL(f);
    const img = document.createElement("img"); img.src = url;
    photosDiv.appendChild(img);
  });
};

async function uploadPhoto(file){
  const fd = new FormData(); fd.append("file", file);
  const r = await fetch(`${BASE}/api/upload/photo`, { method:"POST", body: fd });
  if(!r.ok) throw new Error("photo upload failed");
  return r.json();
}
async function uploadAudio(blob){
  const fd = new FormData(); 
  const file = new File([blob], "voice.webm", {type:"audio/webm"});
  fd.append("file", file);
  const r = await fetch(`${BASE}/api/upload/audio`, { method:"POST", body: fd });
  if(!r.ok) throw new Error("audio upload failed");
  return r.json();
}
async function postNotes(sid, notes){
  const r = await fetch(`${BASE}/api/sessions/${sid}`, {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({ notes })
  });
  if(!r.ok) throw new Error("notes post failed");
  return r.json();
}
async function suggestQuote(sid){
  const r = await fetch(`${BASE}/api/sessions/${sid}/suggest_quote`, { method:"POST" });
  if(!r.ok) throw new Error("suggest quote failed");
  return r.json();
}

sendBtn.onclick = async ()=>{
  try{
    sendBtn.disabled = true;
    log("Sending assets…");

    let sid = null;

    // 1) photos
    for(const f of [...photoEl.files]){
      const res = await uploadPhoto(f);
      sid = sid || res.session_id;
      log("Photo ok: "+res.filename);
    }

    // 2) audio
    if(audioBlob){
      const res = await uploadAudio(audioBlob);
      sid = sid || res.session_id;
      log("Audio ok: "+res.filename+(res.transcript_error? " (transcript error)" : " (transcribed)"));
    }

    // 3) notes
    const notes = notesEl.value.trim();
    if(sid && notes){
      await postNotes(sid, notes);
      log("Notes attached.");
    }

    if(!sid){
      log("Nothing to send — add a photo, a recording, or notes.");
      sendBtn.disabled = false; return;
    }

    // 4) AI suggest quote
    log("Asking AI to draft quote…");
    await suggestQuote(sid);
    log("AI quote ready. Open Dashboard to review.");

    alert("Sent! Quote drafted. Tap 'Open Dashboard' to review.");
  }catch(e){
    log("Error: "+e.message);
    alert("Send failed: "+e.message);
  }finally{
    sendBtn.disabled = false;
  }
}
