'use strict';
// The only script served to a signed-out visitor: it sends the passphrase and goes to the viewer.
const form = document.getElementById('form');
const input = document.getElementById('passphrase');
const button = document.getElementById('go');
const message = document.getElementById('message');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  message.textContent = '';
  button.disabled = true;
  try {
    const response = await fetch('/api/login', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ passphrase: input.value }),
    });
    if (response.ok) { location.assign('/viewer'); return; }
    const reply = await response.json().catch(() => ({}));
    message.textContent = typeof reply.error === 'string' ? reply.error : `Sign-in failed (${response.status})`;
    input.select();
  } catch (error) {
    message.textContent = 'Could not reach the server. Try again.';
  } finally {
    button.disabled = false;
  }
});
