/**
 * Google Apps Script — приём заказов от Telegram-бота «Тариф-Мастер».
 *
 * ===== УСТАНОВКА =====
 * 1. Создайте новую Google-таблицу (sheets.google.com → Создать).
 * 2. В ней: Расширения → Apps Script.
 * 3. Удалите содержимое файла Code.gs и вставьте туда весь код ниже.
 * 4. Развернуть (Deploy) → Новое развёртывание → шестерёнка рядом с "Тип" →
 *    Веб-приложение.
 *    - Execute as: Я (Me).
 *    - Who has access: Все (Anyone) — иначе бот не сможет достучаться без
 *      авторизации Google.
 * 5. Нажмите Развернуть, подтвердите разрешения (если Google предупредит про
 *    неверифицированное приложение — это нормально для собственного скрипта,
 *    Advanced → Go to project (unsafe) → Allow).
 * 6. Скопируйте выданную ссылку (заканчивается на /exec) и вставьте её в
 *    bot.py в переменную GOOGLE_SHEETS_URL (или в переменную окружения
 *    GOOGLE_SHEETS_URL).
 * 7. При последующих правках этого кода не забывайте создавать НОВУЮ версию
 *    развёртывания (Deploy → Manage deployments → Edit → New version) —
 *    иначе изменения не применятся к уже опубликованной ссылке.
 *
 * ===== ЧТО ДЕЛАЕТ =====
 * - Лист «Заказы»: одна строка на заказ. Если заказ с таким номером уже
 *   есть — обновляет её (A-K), не трогая колонку L «Комментарий» — туда
 *   можно дописывать заметки прямо в таблице, бот их не затирает.
 * - Лист «Отчёты»: создаётся автоматически, пересчитывается при каждом
 *   заказе — сводная статистика по всем заказам в таблице.
 */

const SHEET_NAME = 'Заказы';
const REPORTS_SHEET_NAME = 'Отчёты';

const ORDERS_HEADERS = [
  'Номер заказа', 'Дата создания', 'Имя', 'Телефон', 'Telegram ID',
  'Оператор', 'Тариф', 'Сумма', 'Статус', 'Дата оплаты', 'Дата подключения', 'Комментарий',
];

function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);
    const sheet = getOrCreateOrdersSheet_();

    const values = [
      '#' + data.order_number,
      data.created_at || '',
      data.name || '',
      data.phone || '',
      data.telegram_id || '',
      data.operator || '',
      data.tariff || '',
      data.price || '',
      data.status || '',
      data.paid_at || '',
      data.connected_at || '',
    ];

    const existingRow = findRowByOrderNumber_(sheet, data.order_number);

    if (existingRow) {
      // Обновляем A-K, колонку L (Комментарий) не трогаем — её можно
      // заполнять вручную прямо в таблице.
      sheet.getRange(existingRow, 1, 1, values.length).setValues([values]);
    } else {
      values.push(data.comment || '');
      sheet.appendRow(values);
    }

    updateReportsSheet_();

    return ContentService
      .createTextOutput(JSON.stringify({ ok: true }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ ok: false, error: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

function getOrCreateOrdersSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sheet = ss.getSheetByName(SHEET_NAME);

  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
  }
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(ORDERS_HEADERS);
  }

  return sheet;
}

function findRowByOrderNumber_(sheet, orderNumber) {
  const label = '#' + orderNumber;
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return null;

  const columnA = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
  for (let i = 0; i < columnA.length; i++) {
    if (columnA[i][0] === label) {
      return i + 2; // +2: пропускаем заголовок, переходим к 1-based номеру строки
    }
  }
  return null;
}

function updateReportsSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const ordersSheet = ss.getSheetByName(SHEET_NAME);
  let reportsSheet = ss.getSheetByName(REPORTS_SHEET_NAME);

  if (!reportsSheet) {
    reportsSheet = ss.insertSheet(REPORTS_SHEET_NAME);
  }

  const lastRow = ordersSheet.getLastRow();
  if (lastRow < 2) return;

  const rows = ordersSheet.getRange(2, 1, lastRow - 1, ORDERS_HEADERS.length).getValues();

  let total = rows.length;
  let newCount = 0, paidCount = 0, connectedCount = 0, declinedCount = 0, revenue = 0;

  rows.forEach(function (row) {
    const status = row[8];   // колонка I
    const price = Number(row[7]) || 0; // колонка H

    if (status === 'new') {
      newCount++;
    } else if (status === 'paid') {
      paidCount++;
      revenue += price;
    } else if (status === 'connected') {
      connectedCount++;
      revenue += price;
    } else if (status === 'declined') {
      declinedCount++;
    }
  });

  const successful = paidCount + connectedCount;
  const conversion = total > 0 ? Math.round((successful / total) * 100) : 0;
  const avgCheck = successful > 0 ? Math.round(revenue / successful) : 0;

  reportsSheet.clear();
  reportsSheet.appendRow(['Метрика', 'Значение']);
  reportsSheet.appendRow(['Всего заказов', total]);
  reportsSheet.appendRow(['Новых', newCount]);
  reportsSheet.appendRow(['Оплачено', paidCount]);
  reportsSheet.appendRow(['Подключено', connectedCount]);
  reportsSheet.appendRow(['Отказов', declinedCount]);
  reportsSheet.appendRow(['Выручка, ₽', revenue]);
  reportsSheet.appendRow(['Конверсия, %', conversion]);
  reportsSheet.appendRow(['Средний чек, ₽', avgCheck]);
  reportsSheet.appendRow(['Обновлено', new Date()]);
}
