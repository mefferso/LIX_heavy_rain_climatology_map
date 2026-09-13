/* Load the original climatology app, then the optional observation-support overlay. */
(() => {
  const core = document.createElement('script');
  core.src = 'app-core.js';
  core.onload = () => {
    const density = document.createElement('script');
    density.src = 'density.js';
    document.body.appendChild(density);
  };
  document.body.appendChild(core);
})();
