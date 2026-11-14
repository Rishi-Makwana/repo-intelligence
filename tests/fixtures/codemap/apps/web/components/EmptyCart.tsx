export function EmptyCart({ count }: { count: number }) {
  return (
    <section aria-label="Shopping cart">
      <h2>Your cart is empty</h2>
      <p>
        Add something to get started
      </p>
      <input placeholder="Search products" className="w-full bg-white" />
      <span>{count}</span>
      <button>Your cart is empty</button>
    </section>
  );
}
