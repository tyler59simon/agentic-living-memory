package com.acme.billing;

import java.math.BigDecimal;
import java.util.List;

/**
 * Issues and settles invoices.
 */
public class InvoiceService {

    private final LedgerClient ledger;

    public InvoiceService(LedgerClient ledger) {
        this.ledger = ledger;
    }

    /**
     * Creates an invoice for the order and books the receivable in the ledger.
     */
    public Invoice issueInvoice(Order order) {
        Invoice invoice = new Invoice(order.getId(), order.total());
        ledger.record("INVOICE_ISSUED", invoice.getId());
        return invoice;
    }

    /**
     * Loyalty customers get a flat twelve percent discount on invoices above the premium threshold.
     */
    public BigDecimal applyLoyaltyDiscount(Invoice invoice) {
        if (invoice.total().compareTo(PREMIUM_THRESHOLD) > 0) {
            return invoice.total().multiply(new BigDecimal("0.88"));
        }
        return invoice.total();
    }

    public void markPaid(Invoice invoice, List<Payment> payments) {
        for (Payment payment : payments) {
            ledger.record("PAYMENT_APPLIED", payment.getId());
        }
        invoice.setStatus(Status.PAID);
    }

    private static final BigDecimal PREMIUM_THRESHOLD = new BigDecimal("1000");
}
