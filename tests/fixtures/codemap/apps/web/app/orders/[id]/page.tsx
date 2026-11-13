export default function OrderPage({ params }: { params: { id: string } }) {
  return <div>{params.id}</div>;
}
