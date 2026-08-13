import type { ReactNode } from 'react';

interface Props {
  title: string;
  onClose: () => void;
  /** 닫으면 안 되는 팝업(진행 중인 작업 등)은 false — 닫기 버튼을 아예 감춘다. */
  closable?: boolean;
  children: ReactNode;
}

export default function Modal({ title, onClose, closable = true, children }: Props) {
  return (
    // 바깥을 눌러도 닫지 않는다 — 입력하던 내용이 클릭 한 번에 사라지는 사고를 막는다.
    // 닫는 길은 우상단 버튼 하나뿐이다.
    <div className="modal-backdrop">
      <div className="modal">
        <div className="modal-head">
          <h3>{title}</h3>
          {closable && (
            <button type="button" className="modal-close" onClick={onClose} title="닫기" aria-label="닫기">
              ✕
            </button>
          )}
        </div>
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
