import React, { Suspense, lazy } from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { AdminLanguageProvider, StatusLanguageProvider, t } from './i18n/admin';
import './styles.css';

const isAdmin = window.location.pathname.startsWith('/admin');
const AdminApp = lazy(() => import('./AdminApp'));

function AdminRoot() {
  return (
    <AdminLanguageProvider>
      <Suspense fallback={<main className="route-loading" role="status">{t('正在加载')}</main>}>
        <AdminApp />
      </Suspense>
    </AdminLanguageProvider>
  );
}

function StatusRoot() {
  return <StatusLanguageProvider><App /></StatusLanguageProvider>;
}

const Root = isAdmin ? AdminRoot : StatusRoot;
document.title = isAdmin ? '照看室' : '搭档状态';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
