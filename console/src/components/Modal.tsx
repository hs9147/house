import type { ReactNode } from 'react';

interface Props {
  title: string;
  onClose: () => void;
  children: ReactNode;
}

export default function Modal({ title, onClose, children }: Props) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        {children}
      </div>
    </div>
  );
}

export function Confirm({
  title,
  message,
  confirmLabel = '확인',
  danger = false,
  busy = false,
  onConfirm,
  onClose,
}: {
  title: string;
  message: string;
  confirmLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Modal title={title} onClose={onClose}>
      <p style={{ whiteSpace: 'pre-wrap' }}>{message}</p>
      <div className="row" style={{ justifyContent: 'flex-end', marginTop: 16 }}>
        <button className="secondary" onClick={onClose} disabled={busy}>
          취소
        </button>
        <button className={danger ? 'danger' : ''} onClick={onConfirm} disabled={busy}>
          {busy ? '처리 중...' : confirmLabel}
        </button>
      </div>
    </Modal>
  );
}
