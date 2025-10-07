javascript:(function() {
  try {
    const songRows = document.querySelectorAll('[data-testid="song-row"]');

    if (!songRows.length) {
      throw new Error("No song rows found.");
    }

    const downloadLinks = [];
    const titleCounts = {}; // Keep track of how many times each title appears.
    const seenIds = new Set(); // Track clip IDs to avoid duplicates.

    songRows.forEach(row => {
      const clipId = row.getAttribute('data-clip-id');
      const titleElement = row.querySelector('.font-sans.text-base.font-medium.line-clamp-1.break-all.text-foreground-primary');

      if (clipId && titleElement) {
        // Skip duplicate clip IDs
        if (seenIds.has(clipId)) {
          return;
        }
        seenIds.add(clipId);
        let songTitle = titleElement.textContent.trim();
        songTitle = songTitle.replace(/[\\/:*?"<>|]/g, '');

        // Check if the title has been used before.
        if (titleCounts[songTitle]) {
          titleCounts[songTitle]++;
          songTitle += `_${String(titleCounts[songTitle] - 1).padStart(2, '0')}`; // Add _01, _02, etc.
        } else {
          titleCounts[songTitle] = 1; // Initialize the count for this title.
        }

        const mp3Url = `https://cdn1.suno.ai/${clipId}.mp3`;

        downloadLinks.push(`${songTitle}.mp3|${mp3Url}`);
      } else {
        console.warn("Song row found without a clip ID or title element:", row);
      }
    });

    if (!downloadLinks.length) {
      throw new Error("No download links could be constructed.");
    }

    copy(downloadLinks.join('\n'));
    alert(`Copied ${downloadLinks.length} download links to clipboard (with unique filenames).`);

  } catch (error) {
    console.error("Suno Download Link Scraper Error:", error);
    alert("Error: " + error.message);
  }
})()
