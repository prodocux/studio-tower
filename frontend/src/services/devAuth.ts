import { authManager, currentUserSignal } from './auth';
import { api } from './api';
import { showToast } from './toast';

export async function loginAsDevUser(uid: string, email: string, name: string): Promise<void> {
  try {
    const devToken = `dev:${uid}:${email}:${name}`;
    authManager.setDevToken(devToken);
    const me = await api.getMe();
    currentUserSignal.value = me;
    showToast(`Switched account to ${name}`, 'success');
  } catch (err: any) {
    showToast(`Dev login failed: ${err.message}`, 'error');
  }
}
