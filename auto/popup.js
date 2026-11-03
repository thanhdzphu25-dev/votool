let clicks = 0;
let resetTimer;

document.addEventListener("click", () => {
	clicks += 1;
	clearTimeout(resetTimer);
	resetTimer = setTimeout(() => { clicks = 0; }, 1500);
	if (clicks >= 3) {
		clicks = 0;
		chrome.runtime.openOptionsPage();
		window.close();
	}
});
