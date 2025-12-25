javascript: (function () {
  try {
    // Find all song links using the new URL pattern /song/{UUID}
    const songLinks = document.querySelectorAll('a[href^="/song/"]');

    if (!songLinks.length) {
      throw new Error("No song links found.");
    }

    const downloadLinks = [];
    const titleCounts = {};
    const seenIds = new Set();

    songLinks.forEach(link => {
      // Extract UUID from href="/song/{UUID}"
      const href = link.getAttribute('href');
      const match = href.match(/\/song\/([a-f0-9-]{36})/);
      if (!match) return;

      const clipId = match[1];

      // Skip duplicates
      if (seenIds.has(clipId)) return;
      seenIds.add(clipId);

      // Get title from link text
      let songTitle = link.textContent.trim();
      if (!songTitle) return;

      // Sanitize filename
      songTitle = songTitle.replace(/[\\/:*?"<>|]/g, '');

      // Handle duplicate titles
      if (titleCounts[songTitle]) {
        titleCounts[songTitle]++;
        songTitle += `_${String(titleCounts[songTitle] - 1).padStart(2, '0')}`;
      } else {
        titleCounts[songTitle] = 1;
      }

      const mp3Url = `https://cdn1.suno.ai/${clipId}.mp3`;
      downloadLinks.push(`${songTitle}.mp3|${mp3Url}`);
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
