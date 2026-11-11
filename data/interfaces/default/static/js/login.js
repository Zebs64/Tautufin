// Connexion : bascule entre compte Jellyfin et compte local.
document.querySelectorAll('.tab').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('login-mode').value = btn.dataset.tab;
  document.getElementById('login-hint').textContent =
    btn.dataset.tab === 'jellyfin' ? 'Utilisez vos identifiants Jellyfin habituels.' : '';
}));
