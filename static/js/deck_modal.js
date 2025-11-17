let currentRank = null;

function openDeckModal(button) {
  currentRank = button.dataset.rank;
  document.getElementById('deckModal').style.display = 'block';
  document.getElementById('deckModalTitle').textContent =
    (button.textContent.includes("Edit") ? "Edit Deck for Rank " : "Add Deck for Rank ") + currentRank;

  document.getElementById('deckName').value = document.getElementById('deckName_' + currentRank).value || "";
  document.getElementById('deckList').value = document.getElementById('deckList_' + currentRank).value || "";
}

function closeDeckModal() {
  document.getElementById('deckModal').style.display = 'none';
}

function saveDeckTemp() {
  if (!currentRank) return;
  const deckName = document.getElementById('deckName').value;
  const deckList = document.getElementById('deckList').value;

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
