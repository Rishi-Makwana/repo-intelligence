package com.example;

@RestController
@RequestMapping("/invoices")
public class InvoiceController {

    @GetMapping("/{id}")
    public Invoice get(@PathVariable String id) {
        return null;
    }
}
