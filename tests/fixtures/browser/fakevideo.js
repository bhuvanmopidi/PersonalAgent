// A <video> whose playback state we control (headless test browsers can't decode real media).
// window.FAKE_DURATION: seconds (default 1 hour); Infinity = a live stream.
window.fakeVideo = function (video) {
  let paused = true, time = 0;
  Object.defineProperty(video, "paused", { get: () => paused });
  Object.defineProperty(video, "ended", { get: () => false });
  Object.defineProperty(video, "duration", { get: () => window.FAKE_DURATION ?? 3600 });
  Object.defineProperty(video, "currentTime", { get: () => time, set: (t) => { time = t; } });
  video.play = () => { paused = false; time = Math.max(time, 1); return Promise.resolve(); };
  video.pause = () => { paused = true; };
  return video;
};
