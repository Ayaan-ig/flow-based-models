import os
import torch
import torch.nn as nn
import numpy as np
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms
from PIL import Image
import glob
from tqdm import tqdm
import matplotlib.pyplot as plt

from torch.distributions.transformed_distribution import TransformedDistribution
from torch.distributions.uniform import Uniform
from torch.distributions.transforms import SigmoidTransform, AffineTransform

device = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f"Using device: {device}")
torch.manual_seed(0)


class CatCIFAR10Dataset(Dataset):
    def __init__(self, root_dir, transform=None, max_images=50000):
        all_image_paths = sorted(glob.glob(os.path.join(root_dir, '*.png')))
        self.image_paths = all_image_paths[:max_images]

        if len(self.image_paths) == 0:
            raise ValueError("No images found in the dataset directory.")

        self.transform = transform

    def __len__(self):
        return len(self.image_paths)

    def __getitem__(self, idx):
        img = Image.open(self.image_paths[idx]).convert('RGB')
        if self.transform:
            img = self.transform(img)
        return img, 0  # Dummy label


transform = transforms.Compose([
    transforms.Resize((32, 32)),
    transforms.ToTensor(),
    transforms.Lambda(lambda x: x + torch.rand_like(x) / 255.),  
    transforms.Lambda(lambda x: x.view(-1))  
])


cifar10_cat_root = '/kaggle/input/cifar10-pngs-in-folders/cifar10/train/cat'
train_dataset = CatCIFAR10Dataset(root_dir=cifar10_cat_root, transform=transform, max_images=50000)
train_loader = DataLoader(train_dataset, batch_size=64, shuffle=True, num_workers=2)

os.makedirs('Imgs', exist_ok=True)


class StandardLogisticDistribution:
    def __init__(self, data_dim=3 * 32 * 32, device='cpu'):
        self.m = TransformedDistribution(
            Uniform(torch.zeros(data_dim, device=device),
                    torch.ones(data_dim, device=device)),
            [SigmoidTransform().inv, AffineTransform(torch.zeros(data_dim, device=device),
                                                     torch.ones(data_dim, device=device))]
        )

    def log_pdf(self, z):
        return self.m.log_prob(z).sum(dim=1)

    def sample(self, num_samples=1):
        return self.m.sample((num_samples,))


class NICE(nn.Module):
    def __init__(self, data_dim=3 * 32 * 32, hidden_dim=1000):
        super().__init__()
        self.m = torch.nn.ModuleList([nn.Sequential(
            nn.Linear(data_dim // 2, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, data_dim // 2), ) for i in range(4)])
        self.s = torch.nn.Parameter(torch.randn(data_dim))

    def forward(self, x):
        x = x.clone()
        for i in range(len(self.m)):
            x_i1 = x[:, ::2] if (i % 2) == 0 else x[:, 1::2]
            x_i2 = x[:, 1::2] if (i % 2) == 0 else x[:, ::2]
            h_i1 = x_i1
            h_i2 = x_i2 + self.m[i](x_i1)
            x = torch.empty(x.shape, device=x.device)
            x[:, ::2] = h_i1
            x[:, 1::2] = h_i2
        z = torch.exp(self.s) * x
        log_jacobian = torch.sum(self.s)
        return z, log_jacobian

    def invert(self, z):
        x = z.clone() / torch.exp(self.s)
        for i in range(len(self.m) - 1, -1, -1):
            h_i1 = x[:, ::2]
            h_i2 = x[:, 1::2]
            x_i1 = h_i1
            x_i2 = h_i2 - self.m[i](x_i1)
            x = torch.empty(x.shape, device=x.device)
            x[:, ::2] = x_i1 if (i % 2) == 0 else x_i2
            x[:, 1::2] = x_i2 if (i % 2) == 0 else x_i1
        return x


def training(normalizing_flow, optimizer, dataloader, distribution, nb_epochs=10, device='cpu', start_epoch=0, training_loss=None):
    if training_loss is None:
        training_loss = []

    for epoch in tqdm(range(start_epoch, nb_epochs)):
        for batch, _ in dataloader:
            batch = batch.to(device)
            z, log_jacobian = normalizing_flow(batch)
            log_likelihood = distribution.log_pdf(z) + log_jacobian
            loss = -log_likelihood.sum()

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            training_loss.append(loss.item())

        torch.save({
            'epoch': epoch,
            'model_state_dict': normalizing_flow.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            'loss': training_loss,
        }, 'checkpoint.pth')
        print(f"Epoch {epoch + 1}/{nb_epochs} saved.")

    return training_loss


if __name__ == '__main__':
    data_dim = 3 * 32 * 32
    normalizing_flow = NICE(data_dim=data_dim).to(device)
    logistic_distribution = StandardLogisticDistribution(data_dim=data_dim, device=device)
    optimizer = torch.optim.Adam(normalizing_flow.parameters(), lr=0.001, weight_decay=0.9)

    checkpoint_path = 'checkpoint.pth'
    start_epoch = 0
    training_loss = []

    if os.path.exists(checkpoint_path):
        checkpoint = torch.load(checkpoint_path, map_location=device)
        normalizing_flow.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        training_loss = checkpoint['loss']
        start_epoch = checkpoint['epoch'] + 1
        print(f"Checkpoint loaded. Resuming from epoch {start_epoch}")
    else:
        print("No checkpoint found. Starting from scratch.")


    training_loss = training(normalizing_flow, optimizer, train_loader, logistic_distribution,
                             nb_epochs=1500, device=device, start_epoch=start_epoch, training_loss=training_loss)

    # Generate synthetic images
    nb_data = 5
    fig, axs = plt.subplots(nb_data, nb_data, figsize=(10, 10))

    for i in range(nb_data):
        for j in range(nb_data):
            sample = logistic_distribution.sample(num_samples=1).to(device)
            x = normalizing_flow.invert(sample)
            img = x.view(3, 32, 32).permute(1, 2, 0).detach().cpu().numpy().clip(0, 1)
            axs[i, j].imshow(img)
            axs[i, j].set_xticks([])
            axs[i, j].set_yticks([])

    plt.savefig('Imgs/Generated_CIFAR10_Cats.png')
    plt.show()
