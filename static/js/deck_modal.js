let currentRank = null;

function normalizeEditor() {
  const editor = document.getElementById('deckEditor');
  if (!editor) return;
  
  Array.from(editor.childNodes).forEach(node => {
    if (node.nodeType === Node.TEXT_NODE) {
      const div = document.createElement("div");
      const span = document.createElement("span");
      span.textContent = node.textContent;
      div.appendChild(span);
      editor.replaceChild(div, node);
    } else if (node.nodeType === Node.ELEMENT_NODE && node.tagName !== "DIV") {
      const div = document.createElement("div");
      const span = document.createElement("span");
      span.textContent = node.textContent;
      div.appendChild(span);
      editor.replaceChild(div, node);
    } else if (node.nodeType === Node.ELEMENT_NODE && node.tagName === "DIV") {
      let span = node.querySelector("span");
      if (!span) {
        span = document.createElement("span");
        span.textContent = node.textContent;
        node.innerHTML = "";
        node.appendChild(span);
      }
    }
  });
  if (!editor.firstChild) {
    const div = document.createElement("div");
    const span = document.createElement("span");
    div.appendChild(span);
    editor.appendChild(div);
  }
}

function setEditorContent(text) {
  const editor = document.getElementById('deckEditor');
  if (!editor) return;
  
  editor.innerHTML = "";
  const lines = text.split('\n');
  lines.forEach(line => {
    const div = document.createElement("div");
    const span = document.createElement("span");
    span.textContent = line;
    div.appendChild(span);
    editor.appendChild(div);
  });
  normalizeEditor();
}

function getEditorContent() {
  const editor = document.getElementById('deckEditor');
  if (!editor) return "";
  
  const lines = Array.from(editor.children).map(div => {
    const span = div.querySelector("span");
    return span ? span.textContent : "";
  });
  return lines.join("\n");
}

function openDeckModal(button) {
  currentRank = button.dataset.rank;
  document.getElementById('deckModal').style.display = 'block';
  document.getElementById('deckModalTitle').textContent =
    (button.textContent.includes("Edit") ? "Edit Deck for Rank " : "Add Deck for Rank ") + currentRank;

  document.getElementById('deckName').value = document.getElementById('deckName_' + currentRank).value || "";
  
  const deckListValue = document.getElementById('deckList_' + currentRank).value || "";
  setEditorContent(deckListValue);
}

function closeDeckModal() {
  document.getElementById('deckModal').style.display = 'none';
}

function saveDeckTemp() {
  if (!currentRank) return;
  const deckName = document.getElementById('deckName').value;
  const deckList = getEditorContent();

  document.getElementById('deckName_' + currentRank).value = deckName;
  document.getElementById('deckList_' + currentRank).value = deckList;

  const row = document.querySelector(`#standingsTable tr:nth-child(${parseInt(currentRank,10)+1})`);
  const btnDiv = row.querySelector(".deck-buttons");
  btnDiv.innerHTML = `
    <button type="button" class="btn-deck" data-rank="${currentRank}" onclick="openDeckModal(this)">
      <i class="material-icons">edit</i> Edit
    </button>
    <button type="button" class="btn-deck" onclick="removeDeck(${currentRank})">
      <i class="material-icons">delete</i> Delete
    </button>
  `;
  closeDeckModal();
}

function removeDeck(rank) {
  const row = document.querySelector(`#standingsTable tr:nth-child(${parseInt(rank,10)+1})`);
  const btnDiv = row.querySelector(".deck-buttons");
  btnDiv.innerHTML = `
    <button type="button" class="btn-deck" data-rank="${rank}" onclick="openDeckModal(this)">
      <i class="material-icons">add</i> Add Deck
    </button>
  `;
  document.getElementById('deckName_' + rank).value = "";
  document.getElementById('deckList_' + rank).value = "";
}

// Initialize editor handlers
document.addEventListener('DOMContentLoaded', function() {
  const editor = document.getElementById('deckEditor');
  const hidden = document.getElementById('deckList');
  
  if (editor && hidden) {
    normalizeEditor();
    
    // Update hidden field on input, but DON'T call normalizeEditor (causes cursor jump)
    editor.addEventListener("input", () => {
      const lines = Array.from(editor.children).map(div => {
        const span = div.querySelector("span");
        return span ? span.textContent : div.textContent || "";
      });
      hidden.value = lines.join("\n");
    });
    
    editor.addEventListener("keydown", e => {
      if (e.key === "Enter") {
        setTimeout(() => normalizeEditor(), 0);
      }
    });
    
    editor.addEventListener("paste", e => {
      e.preventDefault();
      const text = (e.clipboardData || window.clipboardData).getData("text");
      const lines = text.replace(/\r/g, "").split("\n");
      
      editor.innerHTML = "";
      lines.forEach(line => {
        const div = document.createElement("div");
        const span = document.createElement("span");
        span.textContent = line;
        div.appendChild(span);
        editor.appendChild(div);
      });
      
      normalizeEditor();
      hidden.value = getEditorContent();
    });
  }
});
