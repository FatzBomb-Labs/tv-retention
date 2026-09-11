// What the person at the screen chose, kept per browser.
//
// Remembered here rather than in the settings document because these are preferences
// about *looking* — theme, layout, scale, which bands are open — rather than settings
// about behaviour. They belong to the person at the screen, not to the plugin, and a
// second browser should be free to disagree with the first.
//
// Every read and write is wrapped, because `localStorage` throws rather than returning
// nothing in a private window. A preference that cannot be stored is not an error worth
// showing anybody: the fallback is the answer, and the page carries on.

const KEY = (name) => `tvr.${name}`;

export const remember = (name, value) => { try { localStorage.setItem(KEY(name), value); } catch (error) { /* private window */ } };

export const remembered = (name, fallback) => {
  try { return localStorage.getItem(KEY(name)) || fallback; } catch (error) { return fallback; }
};
